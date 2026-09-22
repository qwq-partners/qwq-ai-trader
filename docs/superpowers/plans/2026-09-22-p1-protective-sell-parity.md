# P1 — 보호 SELL 의 main 동등 (Plan, 2026-09-22 밤 — **Do 착수는 사용자 결정 뒤**)

> 결정 문서 `2026-09-22-kis-judgement-decisions.md` D2 의 방향. P0-4 마감(`1a2cb4d`) 뒤 착수. 기준 HEAD `1a2cb4d`(제품 `80571eb`+P0-4), main 기준선 `origin/main afa6e1e`. 운영 미설치·설치기 제품 호출자 0건 그대로.

## 0. 과정

조사 3(opus/high, 읽기 전용: A main 기준선 · B owner 부품 · C 생산자 입력) → 설계 1(opus/high) → 적대적 심사 2관점(opus/xhigh: ① 돈·상태 손실 ② 실현 가능성·인용) → **둘 다 REVISE**(① must-fix 8: P0 4·P1 4 / ② must-fix 8: P0 2·P1 3·P2 3) → coordinator 처분(§3) → **사용자 결정 요청(§4)** → 확정 단계(§5). 워크플로 `w743ulsy4`, 관측 모델 `claude-opus-5`.

**한 줄:** 설계의 중심은 "보호 SELL 을 처음부터 **시장가**로 내서 취소·3분류·폴백 전체를 작성하지 않는 코드로 만든다"(H6)이다. 두 심사 모두 이 논지가 코드로 지지된다고 봤다(취소 벽 실재: 자식 attempt 는 영구 RECONCILING·파서에 FINAL_CANCELLED 생산 경로 없음). 지적 16건은 전부 그 설계 안의 국소 수정이다. **그러나 H6 은 매매 행동을 바꾸는 돈 경로 결정이라 사용자가 확인해야 한다(§4-1).**

## 1. 조사가 확정한 사실(요약 — 인용은 engine `1a2cb4d`·`main:` 은 `afa6e1e`)

#### 조사 A(main 기준선)

- **B1** (high) 보호 결정(`quote()`의 반환·outbox 행)을 SUBMIT 으로 만드는 소비자는 **코드에 없다**. `quote()` 는 결정이 있으면 outbox 에 `kind='protection_decision', status='pending'` 행을 쓰고 결정 튜플을 반환하지만, 그 행을 읽는 세 곳은 모두 소비자가 아니다 — 배달기는 건너뛰고, market_source 는 교차검증만, intraday_owner 는 같은 종류의 행을 **더 만든다**.
  - src/execution/safety/runtime.py:1245-1252 (outbox 기록)·:1300-1301 (반환) / 건너뜀 src/execution/safety/journal_delivery.py:200-201·:220-222 / 교차검증 src/execution/safety/market_source.py:190 / 생산 src/execution/safety/intraday_owner.py:193-203. 리포지토리 전역 grep `protection_decision` 히트 = 이 4곳 + 시험 2파일뿐
- **B2** (high) `runtime.quote()`/`observe_market()` 의 **제품 호출자는 0건**이다(src/ 전역에서 runtime.py 내부 자기 호출 1건뿐). 호출자는 tests/ 에만 있다.
  - grep `\.observe_market(|runtime\.quote(|\.quote(` over src/ → src/execution/safety/runtime.py:1155 한 줄. tests/test_execution_day_recovery.py:69 등 시험만
- **B3** (high) 차단 21 은 두 skip 이 정본이다 — attach 에서 손절·트레일링·분할익절·갭EOD·보유기간 청산의 **구동기가 0개**다. legacy 의 두 구동기는 attach 에서 조기 return 하고, 세 번째 경로(engine.update_position_price)는 ApplicationBlocked 를 올린다.
  - src/schedulers/kr_scheduler.py:1017-1031(`_check_exit_signal` — 주석이 차단 사유 21 을 이름으로 적고 있다), src/core/batch_analyzer.py:1744-1757(`monitor_positions`), src/core/engine.py:945-947(`update_position_price` → `가격 보호 변경은 execution.quote로 직렬화해야 합니다`)
- **B4** (high) **결정 소비자 없이 결정만 만들면 그 종목의 보호가 영구히 침묵한다**(P0-4 심사 ①M1 의 코드 근거). `quote_protection` 은 새 pending 목표가 생기면 `pending_owners[symbol]=intent_id` 를 세우고, 그 뒤 모든 quote 는 후보에서 `pending_since=now` 로 고정한 채 계산한 뒤 **결정을 None 으로 덮어쓴다**. 게다가 owner 경로는 `_pending_verifier` 가 항상 비어 있지 않아 hard expiry 가 1800초인데, 매 호출이 `pending_since` 를 now 로 고정하므로 그 만료도 **영원히 오지 않는다**. 해제는 실제 SELL 체결(`reduce_protection` 의 `on_fill`) 또는 `repair_protection` 뿐이다.
  - src/execution/safety/protection.py:445-456(pending 포획→`state.pending_since = now`→`decision = None`)·:457-460(pending_owners 설정)·:413-421(체결 시 해제), :283(`manager._pending_verifier = _unknown_pending` — 항상 non-None), src/strategies/exit_manager.py:1283-1292(`_hard_limit = 1800 if self._pending_verifier is not None else 300`)
- **B5** (high) CANCEL 의 전제 ①(요청 조립): `prepare_cancel` 은 `CancelParent` 에 **완성된 OrderRef**(order_no·org_no 비어 있지 않고 `TEMP_`/`local-` 접두 금지), 부모와 **같은 intent_id**·**다른 attempt_id**, `ref.order_date == session.business_date_kst`, market='KR', 같은 account_scope 를 요구한다. 본문은 항상 **전량 취소 고정**(`RVSE_CNCL_DVSN_CD='02'`, `QTY_ALL_ORD_YN='Y'`, `ORD_QTY='0'`), TR `TTTC0803U`. 부분 취소 계약은 존재하지 않는다.
  - src/execution/safety/requests.py:270-294, :126-149(CancelParent 검증), :296-297(MODIFY 는 항상 거부)
- **B6** (high) CANCEL 의 전제 ②(owner 재검사): `_parent` 는 prepare 와 dispatch 에서 각각 부모가 비terminal·`evidence_conflict` 없음·version 일치·`reserved_quantity == parent.remaining_quantity == request.quantity`·`quantity - applied_quantity == remaining_quantity`·**`observed_quantity == applied_quantity`**·예약 4종이 [최소, 원래] 범위 안임을 요구한다. 즉 **관측과 적용이 어긋난 부모는 취소조차 낼 수 없다**.
  - src/execution/safety/commands.py:325-358, 호출 지점 :396-397(prepare)·:529(dispatch 의 `_bound`→`_evaluate`)
- **B7** (high) CANCEL 이 **면제받는 게이트**는 둘뿐이다 — `market_source_pending`(:362-363)과 `reconciler_live`(:367-376)는 SUBMIT/BUY 한정. 그러나 `_owner_ready` 의 `unapplied_execution_observation`(:106-109)과 전역 `unresolved_execution_evidence`(:385-387)는 side·kind 를 가리지 않으므로 **UNKNOWN SUBMIT 1건이 보호 취소까지 막는다**(차단 12).
  - src/execution/safety/commands.py:106-109·:361-363·:367-376·:385-387. 고정 시험 tests/test_execution_owner_gate_authority.py:1004-1013(prepare_cancel → `unresolved_execution_evidence`), 대조 tests/test_execution_market_source.py:441(`market_pending_does_not_add_a_new_alpha_barrier_to_cancel`)
- **B8** (high) CANCEL POST 는 킬스위치를 **거치지 않고**(위험 축소 명령이라 의도적 제외, 현행 `KISBroker.cancel_order` 와 같음) 감사 원장에는 `EV_CANCEL` 로 남는다. 재전송은 없다.
  - src/execution/safety/transport.py:188-198, 모듈 docstring :1-6
- **B9** (high) **취소한 attempt 는 어느 상태에서도 terminal 이 되지 못한다.** `record_result` 는 kind≠submit 이면 status 와 무관하게 `RECONCILING` 을 쓰고, `reconcile()` 은 첫 검사에서 `attempt['kind'] != 'submit'` 을 탈락시킨다. `abandon_candidate` 는 claim 전(미송신) 자식만 끝낸다. 그래서 ACK/UNKNOWN 된 CANCEL 은 **영구 RECONCILING + command_status='acknowledged'/'unknown'** 이고, 그 상태가 그 종목의 모든 새 SUBMIT(`unresolved_child_attempt`)과 일자 전환(`unresolved_child_command`)을 영구히 막는다. 단 **REJECTED 된 취소는 막지 않는다**(두 검사 모두 'rejected' 를 허용).
  - src/execution/safety/lifecycle.py:496-497·:515(`attempt['kind'] != 'submit'` → return)·:391-408(abandon 가드), src/execution/safety/commands.py:382-383, src/execution/safety/day_recovery.py:124-126. 고정 시험 tests/test_execution_child_command_close.py:195(`test_an_acknowledged_child_is_never_closed`)·:225
- **B10** (high) **부모 쪽도 취소 뒤에는 최종화되지 않는다.** 파서는 자식행(내 order_no 를 부모로 가진 행)이 보이거나 읽히지 않은 행이 하나라도 있으면 `chain=True` 로 만들고, `chain` 이면 `supported_finality=False` 다. 게다가 파서는 `FINAL_CANCELLED` 를 **아예 만들지 않는다** — 산출 상태는 전량 체결의 `FINAL_FILLED` 아니면 `RECONCILING` 둘뿐이고, `reconcile` 의 종결 조건은 `evidence.supported_finality` 를 요구한다. 취소를 한 번이라도 보내면 그 체인은 오늘의 파서로 영구 미종결이다(취소 최종성 미지원 = Q7~Q12 판단과 일관).
  - src/execution/safety/evidence.py:243-244(chain 판정)·:264-268(supported)·:279(상태 산출), src/execution/safety/lifecycle.py:550·:555-557(FINAL_CANCELLED 조건은 있으나 파서가 그 상태를 만들지 않는다), docs/superpowers/plans/2026-09-21-s4-owner-path-restoration.md:19-20
- **B11** (high) `prepare_cancel`/`CancelParent` 의 **제품 호출자는 0건**이다(gateway 에 취소 경로가 없다 — `SignalGateway` 는 `prepare_submit` 만 조립한다). 취소는 시험에서만 구동된다.
  - grep `prepare_cancel|CancelParent(` → src/ 히트는 정의부 src/execution/safety/requests.py 뿐, 나머지는 tests/ 11파일. src/execution/safety/gateway.py:159-162(`prepare_submit` 만)
- **B12** (high) late-fill 정확한 시퀀스: BUY 100 중 40 적용 → 보호 SELL 40(보유 전량) → economics 의 `if full:` 이 그 종목 **모든 lot 을 `closed=True`** 로 만들고 포지션·원가행을 지운다 → 잔여 60 이 늦게 체결되면 reconciler 가 `reconcile`(관측 100 기록)은 성공시키지만 `apply` 의 reducer 가 `종결된 진입의 늦은 체결은 대사가 필요합니다` 로 터진다.
  - src/execution/safety/economics.py:441·:458-464(full → lot closed·포지션 삭제), :384-385(늦은 체결 거부), src/execution/safety/lifecycle.py:544-549(reconcile 은 observed 만 올린다)
- **B13** (high) late-fill 의 **정지 낱말은 두 개이고 순서가 있다.** reducer 예외는 `apply` 안에서 삼켜져 receipt `FAILED/'reducer_failed'` 가 되고 inbox 행은 `RECEIVED` 로 **durable 하게 남는다**(receive 는 reducer 앞에서 이미 commit 됨). 그래서 `_evaluate` 는 ① 먼저 `_owner_ready` 에서 **`unapplied_execution_observation`**(inbox 행이 APPLIED/SUPERSEDED 가 아니다 — 종목·side·kind 무관 전역), ② attempt 를 보는 단계에서 **`unresolved_execution_evidence`**(observed 100 ≠ applied 40 — 역시 전역)로 막는다. 일자 전환도 `unapplied_inbox`·`observed_not_applied` 로 BLOCKED. 기존 문서(P0-3 §2, P0-4 F-B1-11)가 `unresolved_execution_evidence` 만 적은 것은 **불완전하다 — 실제로 먼저 닿는 낱말은 `unapplied_execution_observation` 이다.**
  - src/execution/safety/application.py:559-569(reducer 예외 → FAILED)·:479-491(RECEIVED 행의 선행 commit), src/execution/safety/commands.py:106-107(`unapplied_execution_observation`)·:361(그 호출이 attempt 순회보다 앞)·:385-387, src/execution/safety/day_recovery.py:117·:129, 재처리 계수 src/execution/safety/runtime.py:1039-1042(`_reconciler_parked`)
- **B14** (high) F8 ①(같은 종목 반대 side)의 잠금 지점은 한 줄이다 — `_evaluate` 의 종목 잠금이 side 를 보지 않고, 예약 검사(:422-426)보다 **앞**에 있다. 미체결 BUY 가 있으면 같은 종목 보호 SELL 이 `unresolved_symbol_attempt` 로 막힌다. 자식(취소) 가드도 side 를 보지 않는다.
  - src/execution/safety/commands.py:393-394 vs :422-426, 자식 :382-383. 고정 시험 tests/test_execution_p04_reservation.py:137·:168·:183(side 무관 잠금)
- **B15** (high) `_protection_failed` 는 **프로세스 수명 bool 래치이고 해제 코드가 없다**(전 리포지토리 6곳: 초기화 1·설정 2·읽기 3). 설정 지점은 quote task 의 취소 또는 commit 뒤 실패, 그리고 repair 실패. 소비자는 ① `market_source_pending` → SUBMIT 만 차단(보호 SELL 포함, CANCEL 은 통과) ② `_quiescence_reason` → 일자 전환 BLOCKED.
  - src/execution/safety/runtime.py:96·:1310-1311·:1346(설정), :463-466·:206-207·:1479(읽기). 소비 지점 src/execution/safety/commands.py:362-363. P0-4 S-A 가 좁힌 것은 **설정 조건뿐**(사전 거부 면제, runtime.py:1221-1226)
- **B16** (high) durable `protection_quote_admissions` 행은 **성공한 같은 commit 에서만** 지워진다 — 실패·재시작·다음 날 어느 쪽도 지우지 못한다(차단 사유 23). 한 종목에 한 행만 허용되므로 남아 있는 행은 그 종목의 다음 quote 도 거부하고, 동시에 전역적으로 SUBMIT(`market_source_pending`)·일자 전환(`unresolved_quote_admission`)을 막는다.
  - src/execution/safety/runtime.py:1208-1213(접수)·:1266(유일한 삭제)·:1209-1210(종목당 1행)·:465, src/execution/safety/day_recovery.py:119-120, docs/superpowers/plans/2026-09-22-p0-4-latch-reservation-session-stale.md:3-5절 §3-5 차단 23
- **B17** (high) **재개(래치·durable 행 해제)가 안전해지는 조건은 코드에서 정확히 '결정 소비자의 존재'다.** 재개는 결국 같은 `quote()` 를 다시 도는 것이고, 그 성공 commit 이 `pending_stage`+`pending_owners` 를 세운다(B4). 소비자가 없으면 재개는 손절을 **조용히 사라지게** 만들어 legacy 보다 나빠진다. 따라서 해제 설계는 생산자 설계와 같은 설계 안에 있어야 한다(P0-4 가 H1-4 를 폐기한 이유).
  - src/execution/safety/runtime.py:1229-1267(reduce 가 결정과 접수 삭제를 한 commit 에)·protection.py:457-460, docs/superpowers/plans/2026-09-22-p0-4-latch-reservation-session-stale.md §3-1 ①M1·§5-4 'P1 진입 조건'
- **B18** (high) 세션 라벨은 6구간이고 `prepare_submit` 이 `break`/`closed` 를 거부, `pre_close`/`closing` 의 MARKET 도 거부한다. `next_market`(15:40~20:00)과 `pre_market` 은 `ORD_DVSN='05'`+`AFHR_FLPR_YN='Y'` 라 지정가와 시장가가 **`ORD_UNPR`(와 fingerprint)로만 다르다** — 시장가는 `wire_price=0`.
  - src/execution/safety/requests.py:64-72·:243-260. 고정 시험 tests/test_execution_dispatch_reasons.py:621(`test_the_closing_and_next_market_sessions_bake_different_wire_bodies`), 커밋 a4f8585 의 C6 docstring 정정
- **B19** (high) prepare 와 dispatch 사이에 세션이 바뀌면 attempt 는 **durable 하게 종료된다**: `_dispatch` 는 `_request(session=False)` 로 세션 검사를 건너뛰지만 `_bound`→`_evaluate`→`_session` 이 `request_session_changed` 를 내고, `CommandValidationError` 는 `_unsent` → `abandon_candidate` 로 가 `FINAL_REJECTED`·`command_status='not_sent'`·예약 4종 해제로 끝난다.
  - src/execution/safety/commands.py:614·:364·:632-633·:579-599, src/execution/safety/lifecycle.py:412-417. 고정 시험 tests/test_execution_dispatch_reasons.py:547(`...session_boundary_crossed_between_prepare_and_dispatch_ends_the_attempt`)
- **B20** (high) **세션 재준비의 gateway 계약은 이미 성립한다**: `FINAL_REJECTED` 는 TERMINAL_STATES 라 종목 잠금이 풀리고, gateway 는 `(symbol, side, strategy)` 키로 **intent_id 를 재사용**하며 `attempt_id` 만 새로 발급한다. 즉 같은 청산 의도를 유지한 채 다음 세션용 새 attempt 를 만드는 것이 계약 안에서 가능하다. 다만 **재준비를 실행하는 코드는 없다** — `gateway.submit` 의 유일한 호출자는 SIGNAL 하나를 처리하는 `engine._submit_signal` 이므로 재준비의 소유자는 '생산자'다(dispatch 안 재시도·세션 라벨 교체는 금지).
  - src/execution/safety/lifecycle.py:47-50, src/execution/safety/gateway.py:42·:151-162, src/core/engine.py:655-672(유일 호출자). 고정 시험 tests/test_execution_dispatch_reasons.py:582(`test_the_producer_can_reprepare_the_same_intent_in_the_new_session`), tests/test_execution_child_command_close.py:225
- **B21** (medium) gateway 의 intent 표(`self._intents`)는 **메모리 전용**이라 재시작하면 같은 (종목·side·전략)이 새 intent_id 를 받는다. durable intent 등록부는 없다. (영향 판단 — 예: 재시작을 낀 세션 재준비가 같은 청산 의도를 이어간다는 보장이 없다 — 은 추측이다.)
  - src/execution/safety/gateway.py:42(`self._intents: dict[tuple, str] = {}`), 재시작 복원 경로 없음(restore 는 owner state 만: src/execution/safety/runtime.py:137-147)
- **B22** (high) attach 에는 legacy 의 보호 에스컬레이션이 **순회 대상 자체로 존재하지 않는다**: 90초 미체결 SELL 시장가 폴백과 10분 BUY 타임아웃 취소는 `_pending_timestamps` 를 돌지만, attach 에서는 그 장부에 아무도 쓰지 않는다(H7 은 오히려 남아 있으면 거부한다).
  - src/core/engine.py:1991-2078(폴백 루프)·:2484-2490(attach 분기에서 legacy 장부 미기록)·:1983-1989(H7 잔존 거부)
- **B23** (high) main(origin/main `afa6e1e`)의 취소 뒤 3분류는 코드로 확정되어 있다: `stale_order_still_live` 가 True(생존)/False(소멸)/None(판단 불가)를 돌려주고, 호출자는 첫 회는 조회 없이 60초 스로틀만 기다린 뒤 확인하며, 생존이면 상한 없이 유지+1회 경보, **판단 불가 연속 2회(≈등록 후 5분)면 stage 롤백 없이 pending 만 해제**한다. 이 보호는 **분할 청산 pending 에만** 적용되고 전량 청산은 종전대로 즉시 해제한다(근거는 D1 의 미확인 가정).
  - main:src/core/engine.py:2384-2411, main:src/schedulers/kr_scheduler.py:983-1043(특히 :1006 `stale_order_still_live(..., confirm=waited)`·`_MAX_EXIT_UNKNOWN` 분기)
- **B24** (high) engine 브랜치의 `_cleanup_stale_pending` 은 PR #81 **이전 판**이고, P0-4 S-D 가 넣은 것은 attach 조기 return 한 줄뿐이다 — attach 에서는 아무 정리도 하지 않는다(취소·롤백 모두 skip). 즉 attach 는 main 의 3분류를 아직 하나도 갖고 있지 않다.
  - src/schedulers/kr_scheduler.py:918-930(attach 조기 return + '차단 사유 21 과 같은 계열' 주석), 고정 시험 tests/test_execution_p04_stale_pending.py:95·:109·:128
- **B25** (high) reconciler 는 SUBMIT 만 대상으로 돈다는 것이 사실상의 전제다 — `_reapply` 가 되살리는 관측은 `attempt['order_ref']`·`observed_quantity` 로 합성되고, `reconcile` 이 kind='submit' 만 받는다(B9). 따라서 **CANCEL 은 생산자(수집기) 쪽에도 소비 경로가 없다**.
  - src/execution/safety/runtime.py:1003-1011(`_stored_observation`), src/execution/safety/lifecycle.py:515
- **B26** (high) 시험 corpus 가 이미 고정한 것(P1 이 깨면 안 되는 계약): ① 자식 종료 가드 전체 ② 세션 경계 소멸·재준비 ③ 부분 체결의 예약 산술·side 무관 잠금·합성 만료 거부 ④ 접수 래치의 설정 조건 ⑤ UNKNOWN 1건의 전역 정지(CANCEL 포함) ⑥ attach 의 stale pending 무동작.
  - tests/test_execution_child_command_close.py(80·95·111·169·184·195·210·225·271·300·322·341), tests/test_execution_dispatch_reasons.py(413·457·547·582·606·621), tests/test_execution_p04_reservation.py(114·146·191·230·242), tests/test_execution_market_source.py(441·485·522·564), tests/test_execution_owner_gate_authority.py(1002·1013), tests/test_execution_p04_stale_pending.py(95·109·128·245·250)
- **B27** (high) 미고정 갈래(P1 의 RED 후보): ① **결정이 소비되는 갈래가 통째로 0건** — `protection_decision` 행을 읽어 SUBMIT 으로 만드는 시험이 없다 ② ACK 된 CANCEL 이 종결되는 갈래(현행 코드로는 도달 불가라 표본이 없다) ③ late-fill 전 시퀀스(BUY 100/40 → SELL 40 → 잔여 60 도착)는 P0-3 Q-1 이 **P1 의 RED 로 등록만** 해 둔 상태 ④ `reserved_quantity_insufficient` 는 제품 1곳뿐인데 tests/ 히트 0건 ⑤ closing 세션의 보호 SELL 처리(지정가 강등 vs 15:40 대기) 표본 0.
  - docs/superpowers/plans/2026-09-22-p0-3-wiring-sync-gate.md §3 Q-1, docs/superpowers/plans/2026-09-22-p0-4-latch-reservation-session-stale.md §1 F-B4-2·§5-4('닫히지 않은 것'), grep `reserved_quantity_insufficient` → src/execution/safety/commands.py:426 한 곳
- **B28** (medium) P1 이 게이트를 열 때 반드시 같이 다뤄야 할 순서 의존: `commands.py:393-394` 의 side 인지를 먼저 열면 late-fill 이 곧바로 **전역** 정지(B13)를 만든다. 반대로 late-fill 수용(economics 의 closed lot 계약)을 먼저 고치면 side 인지 없이도 잔여 60 은 `reserved_quantity_insufficient` 로 정확히 막히므로 안전 방향이다.
  - src/execution/safety/commands.py:393-394 vs :422-426, src/execution/safety/economics.py:384-385·:458-464, docs/superpowers/plans/2026-09-22-p0-4-latch-reservation-session-stale.md:92(같은 결론)

**checklist**

- src/execution/safety/economics.py:384-385·:458-464 — 전량 청산이 닫은 lot 위의 부모 BUY 잔여 체결을 받는 계약(late-fill). 여기를 먼저 고쳐야 F8 ① 을 여는 것이 안전해진다(B28)
- src/execution/safety/commands.py:393-394 — `unresolved_symbol_attempt` 의 side 인지(미체결 BUY 위의 보호 SELL 허용). 여는 순서는 late-fill 수용 **뒤**
- src/execution/safety/commands.py:382-383 — 자식(CANCEL) 가드의 side 인지·범위. 지금은 ACK 된 취소 1건이 그 종목 보호 SELL 까지 영구 차단(B9)
- src/execution/safety/lifecycle.py:496-497·:515 — CANCEL attempt 의 종결 계약. `record_result` 가 자식을 무조건 RECONCILING 으로 두고 `reconcile` 이 kind='submit' 만 받아 **해제 간선이 0개**다. 취소 최종성을 만들지 않고 자식을 끝내는 길(예: 부모 종결/세션 종료 기반 자식 정리)을 여기서 정해야 한다
- src/execution/safety/evidence.py:243-244·:264-268·:279 — 취소 뒤 `chain=True` 고착과 `FINAL_CANCELLED` 미생산. 3분류(소멸/생존/판단 불가)를 owner 안으로 들이려면 증거 계약의 산출 상태가 늘어나야 한다(취소 최종성을 **인정**하는 것과 구분해 설계)
- src/execution/safety/runtime.py:1245-1252 + 새 소비자 — `protection_decision` outbox 행을 읽어 gateway.submit 으로 보내는 생산자(차단 21). 소비자가 생기기 전에는 B4 때문에 결정을 만드는 것 자체가 위험
- src/execution/safety/protection.py:445-460 — `pending_owners`/`pending_since` 고정의 수명 계약. 소비자가 생긴 뒤 '주문이 실제로 나갔는가'로 pending 을 풀/만료시킬 자리
- src/execution/safety/runtime.py:96·:1310-1311·:1346·:463-466·:206-207 — `_protection_failed` 래치의 해제 경로(차단 23 앞쪽). 해제는 결정 소비자와 같은 설계 안에서만
- src/execution/safety/runtime.py:1208-1213·:1266 — 고아 `protection_quote_admissions` 행의 해제 경로(차단 23 뒤쪽). 재시작·일자 전환에서도 지워지지 않는다
- src/execution/safety/gateway.py:151-165 — 취소 경로(`prepare_cancel` 조립)와 세션 재준비의 소유자. intent 재사용 규칙은 그대로 두되 `_intents` 의 메모리 전용 성질(B21)을 결정에 포함
- src/execution/safety/commands.py:106-109·:385-387 — late-fill/미적용 inbox 의 정지 범위. 전역 정지를 종목 단위로 줄이는 것은 차단 19 범위 축소(P2)와 같은 자리이며, P1 이 먼저 **낱말 두 개**(`unapplied_execution_observation`→`unresolved_execution_evidence`)를 이름 붙여야 한다(B13)
- src/schedulers/kr_scheduler.py:918-930·1017-1031 및 src/core/batch_analyzer.py:1744-1757 — attach 조기 return 을 '구동기 배선'으로 바꾸는 지점(main PR #81 의 3분류는 여기 대응). legacy 바이트 동일 제약 유지
- src/execution/safety/requests.py:243-260 — closing/next_market 의 보호 SELL 처리(지정가 강등 vs 15:40 대기)와 재준비 횟수 상한을 넣을 자리. 지금은 MARKET 이 pre_close/closing 에서 build 거부

**open_questions**

- 결정 소비자(생산자)가 `protection_decision` 행을 읽는가, `quote()` 반환값을 직접 받는가? 행을 읽으면 durable·재시작 복원 가능하지만 소비 완료 표식(status 전이)과 배달기(`journal_delivery` 의 skip 규칙)를 같이 고쳐야 하고, 반환값을 받으면 호출자 취소 창에 결정이 사라진다(`asyncio.shield` 뒤).
- 취소 최종성이 '알 수 없다'인 상태에서 CANCEL attempt 를 종결시킬 **in-contract** 근거는 무엇인가? 후보: (a) 부모가 다른 경로로 terminal 이 되면 자식도 닫는다 (b) 자식은 애초에 attempt 로 만들지 않고 부모 행의 필드로 기록한다 (c) 세션 종료를 근거로 닫는다 — (c) 는 만료 판정을 지어내는 것이라 P0-4 가 거부한 종류(차단 22)와 같은 계열로 보인다.
- main 의 3분류를 attach 로 옮길 때 '거래소 실조회'(`get_exchange_open_orders`)는 owner 의 증거 계약(수집기·파서·query_scope)을 통과해야 하는가, 아니면 보호 판단 전용 read-only 조회로 두는가? 전자는 TR/스키마 계약이 늘고, 후자는 owner 밖 사실을 만드는 것이라 계약 1 과 충돌한다.
- `unresolved_symbol_attempt` 의 side 인지를 열 때, **보유 40주에 대한 보호 SELL** 이 미체결 BUY 60주와 같은 종목에 공존하는 동안 `held − reserved` 는 어떤 값이어야 하는가(BUY 예약은 수량 예약이 아니라 현금 예약이므로 현행 식에서는 충돌하지 않는 것으로 보이나, 시험 표본이 0건이다).
- 차단 23 해제를 P1 에 포함할 때, 고아 접수 행을 '재개'로 풀 것인가 '포기(명시 폐기 + degraded 표시)'로 풀 것인가? 재개는 낡은 진입 증거로 BUY 게이트를 여는 ①M3 위험이 있고, 포기는 그 종목을 degraded 로 만들어 보호 계산이 멈춘다(`reduce_protection` 의 degraded 경로).
- closing(15:20~15:30) 의 보호 SELL 을 지정가로 강등할지 15:40(next_market)까지 대기할지 — 그리고 재준비 횟수 상한 — 은 P0-4 가 사용자 결정으로 넘긴 항목이다(§3-3). P1 설계 전에 결정이 필요한가, 설계 안에서 보수적 기본값(대기·상한 2회 등)으로 위임받을 것인가?
- 실계좌 스모크(P0-3 §5 D10 기록기)는 여전히 사용자 확인 대기다 — 취소 응답·생존 조회의 실제 필드를 못 본 채 3분류를 구현하면 그 부분은 '어느 답이 참이어도 안전한' 쪽으로만 쓸 수 있는가?

#### 조사 B(owner 부품)

- **M-P1** (high) main 의 보호 SELL 신호 생산자는 4개다: WS 틱 콜백, 20초 REST 폴링, 30분 배치, (취소/폴백 전용) 60초 pending 정리 루프. 어느 것도 owner 를 거치지 않고 legacy `exit_manager.update_price` 를 직접 읽는다.
  - main:scripts/run_trader.py:1932-1941 (`_on_market_data` → `kr_scheduler._check_exit_signal(event.symbol, event.close)`) · main:src/schedulers/kr_scheduler.py:4867 (REST 피드가 `market_data={ma5,prev_low,high,low}` 를 붙여 호출) + :4996 (`await asyncio.sleep(20)`) · main:src/core/batch_analyzer.py:1731-1874 (`monitor_positions`) · main:src/schedulers/kr_scheduler.py:5001-5014 (`run_pending_cleanup`, 60초)
- **M-P2** (high) WS 경로는 `market_data` 를 전달하지 않아 복합 트레일링(MA5·전일저가)이 WS 틱에서는 발화하지 않는다 — 같은 청산 판정이 입력 경로에 따라 비대칭이다.
  - main:scripts/run_trader.py:1941 은 인자 2개만 넘긴다(`_check_exit_signal(event.symbol, event.close)`), 반면 REST 는 main:src/schedulers/kr_scheduler.py:4861-4867 에서 `_md` 를 넘긴다. `_check_composite_trailing` 은 main:src/strategies/exit_manager.py:1184-1185 에서 `if market_data is None: return None`.
- **M-E1** (high) 손절 발화: `update_price` 가 `resolve_stop`(전략별 고정 SL·ATR 동적 SL·급락 cap 우선순위)으로 SL%를 정하고 수수료 차감 순손익률이 `-sl_pct` 이하면 `("sell_all", 잔여수량, "손절: …")`. `resolve_stop` 의 ValueError 는 흡수하고 legacy 우선순위로 폴백한다(청산 판정 전체가 스킵되는 fail-open 방지).
  - main:src/strategies/exit_manager.py:1030-1048 (특히 :1043 `if net_pnl_pct <= -sl_pct`, :1035-1042 폴백)
- **M-E2** (high) 트레일링은 두 갈래다. ① breakeven 활성 전: 순손익 ≥ `trailing_activate_pct` 면 고점 대비 `ts_pct` 이탈에서 전량. ② breakeven 활성 후: `max(ATR×1.5, effective_trailing)` (급락 시 `INTRADAY_CRASH_PARAMS` 의 TS 로 상한 캡), 단 stage 가 FIRST/SECOND 면 전량 청산 대신 **고점을 현재가로 리셋**하고 THIRD/TRAILING·코어에서만 전량. 여기에 stage 별 본전 이탈 버퍼(코어 -2%/FIRST·SECOND -0.5%/그 외 KR 0.25%)가 별도 전량 청산을 낸다.
  - main:src/strategies/exit_manager.py:1149-1162 (①), :1087-1124 (②, 특히 :1100-1104 급락 캡·:1110-1123 stage 분기), :1127-1147 (본전 이탈)
- **M-E3** (high) 분할 익절은 `_check_partial_exit` 가 3단계로 내며 `pending_stage` 로 중복을 막는다. verifier 배선 시 하드 만료는 1800초, 미배선(레거시/US)이면 300초.
  - main:src/strategies/exit_manager.py:1050-1054, :1246-1286 (특히 :1276 `_hard_limit = 1800 if self._pending_verifier is not None else 300`), :1291-1340 (stage 별 pending_stage 세팅)
- **M-E4** (high) 보유기간·횡보·익절후 저효율·추세 무효화 4종의 `sell_all` 이 손절보다 **앞에서** 판정된다 — 즉 보유기간 초과가 손절보다 우선이다. 보유기간은 영업일 기준이고 포지션별 `max_holding_days` 가 글로벌보다 우선, 0 은 무제한.
  - main:src/strategies/exit_manager.py:954-1023 (:960-968 보유기간, :980-989 횡보, :991-1008 익절후 저효율, :1010-1023 추세 무효화) — 손절은 그 뒤 :1043
- **M-E5** (high) batch_analyzer 에는 ExitManager 와 **별개의** 보유기간 강제 청산이 하나 더 있다 — 달력일(`(datetime.now() - pos.entry_time).days`) 기준이고 `self._max_holding_days` 를 쓴다. ExitManager 의 영업일 기준과 다른 기준이 같은 목적으로 병존한다.
  - main:src/core/batch_analyzer.py:1842-1866 (:1846 `holding_days = (datetime.now() - pos.entry_time).days`) vs main:src/strategies/exit_manager.py:958 `self._count_business_days(...)`
- **M-E6** (high) 갭EOD·테마EOD 는 ExitManager 가 아니라 `_check_exit_signal` 안의 시각 문자열 비교로 발화한다: `_now_hm >= "15:10"` 이고 gap_and_go 손익 < 0%(갭) / theme_chasing 손익 < +1%(테마) 면 전량 SELL emit + 동기 pending 등록 후 `return`.
  - main:src/schedulers/kr_scheduler.py:1114-1141 (테마EOD), :1143-1172 (갭EOD)
- **M-E7** (high) RSI2 전용 청산(RSI(2)>70)은 30분 배치에만 있고 `metadata` 에 `quantity` 를 넣지 않는다 — 엔진 on_signal 이 `position_size = pos.quantity` 로 전량 처리한다.
  - main:src/core/batch_analyzer.py:1771-1795 (metadata 없음) + main:src/core/engine.py:2056-2057 (`else: position_size = pos.quantity if pos else 0`)
- **M-O1** (high) SELL 주문은 매수1호가 지정가가 기본이고, 호가 조회가 실패하면 시장가로 떨어진다. SELL 은 리스크 검증(`can_open_position`)을 통째로 건너뛴다.
  - main:src/core/engine.py:2078-2101 (`_get_sell_price` → LIMIT, 없으면 MARKET), :2224-2236 (`_get_sell_price`), :2114 (`if order.side == OrderSide.BUY:` 로만 리스크 체크)
- **M-O2** (high) 분할 매도 의도는 **수량을 정한 같은 스냅샷**에서 확정되고(`_sell_partial_intent`), pending 등록 시 `_pending_signal_cache[sym] = {"sell_partial_intent": True}` 로 남는다. `exit_action` 이 `sell_all`·`replacement_exit` 이면 수량과 무관하게 전량으로 읽는다.
  - main:src/core/engine.py:2042-2057 (:2054-2055 판정), :2191-2198 (등록)
- **M-C1** (high) 미체결 SELL 의 폴백 임계는 90초이고 정규장(09:00~15:30)에만 발화한다. 상한은 시장가 폴백 2회이며, 상한에 닿으면 **취소를 보내지 않고** pending 만 해제한다 — 원 지정가가 거래소에 남는다.
  - main:src/core/engine.py:1743-1756 (`_SELL_TIMEOUT = 90`, `is_regular_hours = 900 <= time_val < 1530`), :2252 (`_MAX_FALLBACK = 2`), :2291-2296 (상한 시 `clear_pending` 후 `return`, cancel 호출 없음)
- **M-C2** (high) 취소 뒤 3분류(소멸/생존/판단 불가)는 `stale_order_still_live(symbol, side, confirm=)` 한 함수에 있다: (a) 브로커 인메모리 장부에 그 종목·그 방향의 활성 주문이 없으면 False(소멸), (b) `confirm=False`(첫 회)면 조회 없이 True(생존 간주·한 주기 대기), (c) `confirm=True` 면 거래소 실 미체결(TTTC8036R/TTTC0084R)로 가려 행이 있으면 True, 조회가 None 이면 None(판단 불가), 없으면 False.
  - main:src/core/engine.py:2384-2411
- **M-C3** (high) '판단 불가'의 상한은 **횟수가 아니라 시간**이다: 마지막 비-판단불가 판정 뒤 180초(`_SELL_UNKNOWN_BUDGET_SECONDS`)가 지나도록 판단 불가뿐이면 재주문 없이 pending 을 해제하고 CRITICAL 을 남긴다. 등록 후 90 + 180 = 270초 = 종전 최장 보유. 재시도 간격은 직전 판정이 '거래소 생존'이면 60초, 아니면 20초.
  - main:src/core/engine.py:2437-2440, :2429-2436 (`_SELL_CANCEL_RETRY_SECONDS = 20`, `_SELL_ALIVE_RETRY_SECONDS = 60`, `_sell_retry_interval`), :2487-2494 (예산 초과 해제)
- **M-C4** (high) '거래소 생존이 확인된' SELL 은 **상한 없이 무기한 유지**한다(시장가를 얹지 않는다). 그 유지 중 ERROR 로그는 유지당 1회만 남기고 이후는 헬스 모니터의 교착 경보에 맡긴다.
  - main:src/core/engine.py:2456-2457 (docstring "생존이면 상한 없이 유지"), :2495-2499 (`alive_logged` 1회 제한)
- **M-C5** (high) 취소 실패 유지의 재시도는 SIGNAL 이 없어도 엔진 하트비트(10초, 우선순위 10)가 구동한다. 정규장에만 돌고, 오래 기다린 순으로 처리하되 하트비트 한 번의 처리 시간 상한은 5초(`_HEARTBEAT_RETRY_BUDGET_SECONDS`)다 — 브로커가 느리면 나머지는 다음 하트비트로 밀린다.
  - main:src/core/engine.py:2507-2532 (:2518 정규장 게이트, :2524-2526 due 정렬, :2529-2530 예산), :2441 (상수), :1523 (`engine.register_handler(EventType.HEARTBEAT, self.on_heartbeat)`)
- **M-C6** (high) 동시호가(15:20~15:30)에는 **취소를 보내기 전에** 분기해 원 지정가를 거래소에 남긴다(보유 포지션이 있을 때만). 09-21 이전에는 취소를 먼저 보낸 뒤 '지정가 유지'로 빠져 청산 주문이 재주문 없이 사라졌다.
  - main:src/core/engine.py:2297-2303 (`if 1520 <= time_val < 1530 and _held is not None and _held.quantity > 0: … return`, 주석이 그 버그를 명시)
- **M-C7** (high) 스케줄러 쪽 청산 pending 정리는 별도 상한을 쓴다: stale 기준은 장전 5분/정규장 이후 3분, 취소 재시도는 60초 스로틀, 판단 불가는 **연속 2회**(`_MAX_EXIT_UNKNOWN`, 등록 후 약 5분)면 **stage 롤백 없이** pending 만 해제한다. 취소 API 가 예외를 던진 경우만 15분 초과 시 강제 해제.
  - main:src/schedulers/kr_scheduler.py:864 (`stale_minutes = 5 if now_time.hour < 9 else 3`), :888-890 (60초 스로틀), :975 (`_MAX_EXIT_UNKNOWN = 2`), :1019-1031 (해제), :898-912 (예외 15분)
- **M-C8** (high) 취소 뒤 '살아 있는 주문 위 재발행' 관문은 **분할 매도에만** 걸린다. 엔진은 등록 시점 의도(`sell_partial_intent`), 스케줄러는 `_partial_exit_marks()[sym] == 등록시각` 표식으로 판정하며, 전량 청산(손절·트레일링·EOD)은 그 관문을 통째로 건너뛰고 즉시 시장가 폴백/즉시 해제로 간다.
  - main:src/core/engine.py:2327-2335 (`_partial_intent` 인 경우에만 `_keep_stale_sell_after_failed_cancel`), main:src/schedulers/kr_scheduler.py:1000-1001 (`self._partial_exit_marks().get(s) != pending_ts: return False`), :1203-1207 (표식 기록)
- **M-C9** (high) await 를 건넌 뒤 같은 pending 인지 재확인하는 세대 검사가 양쪽에 있다 — 엔진은 `_is_same_sell_pending`(등록 시각 동일성), 스케줄러는 `_gen`/`pending_ts` 비교. 이것이 없으면 옛 주문의 결과로 새 pending·stage 를 풀어 같은 청산이 중복 발행된다.
  - main:src/core/engine.py:2443-2447, :2318-2319, :2331-2332 · main:src/schedulers/kr_scheduler.py:917-921, :965-966, :1010-1011
- **M-C10** (high) 취소 POST 는 재시도를 유지한다(`_api_post` 기본 `retry=True`). 주문 접수·정정만 `retry=False` 다.
  - main:src/execution/broker/kis_kr.py:912 (취소, retry 인자 없음) vs :553 (접수 `retry=False`), :1009 (정정 `retry=False`)
- **M-G1** (high) exit_exempt(자동매도 금지) 가드는 main 에 **9곳**이다: 엔진 on_signal 중앙 가드, on_order 제출 직전 재검사, 축출 후보 제외, stale SELL 폴백 머리, keep 판정 중 재확인, 스케줄러 `_check_exit_signal` 머리, 스케줄러 keep 판정, `_exempt_sell_still_open`, ExitManager.update_price.
  - main:src/core/engine.py:1835-1847 · :2565-2568 · :1463-1466 · :2259-2288 · :2481-2484 · :2352-2353 / main:src/schedulers/kr_scheduler.py:1058-1059 · :1012-1016 · :844-845 / main:src/strategies/exit_manager.py:938-939
- **M-G2** (high) 면제 종목에 이미 나간 미체결 SELL 은 '취소만 하고 재주문하지 않는다'. 취소 뒤 브로커 장부에 남아 있으면 pending 을 유지하고 60초 뒤 재시도(전용 스로틀 `_exempt_cancel_last_try`, pending 등록 시각은 건드리지 않아 헬스 모니터의 300초 교착 경보가 그대로 발화).
  - main:src/core/engine.py:2257-2288 (:2281-2284 생존 시 유지, :2285-2287 해제)
- **M-G3** (high) SELL 에 대한 쿨다운/차단은 의도적으로 없다: 주문 실패 5분 쿨다운은 BUY 전용이고(SELL 은 항상 통과), 주문 제출 실패 시 `block_symbol` 도 BUY 만 건다. 대신 SELL 의 APBK0400('주문 가능한 수량 초과')이 2회 누적되면 좀비 후보로 마킹해 다음 sync 에서 강제 정리 + 텔레그램 경보.
  - main:src/core/engine.py:2031-2039, :2581-2584, :2586-2600
- **M-G4** (high) 프리장(PRE_MARKET)에서는 손절이 아닌 SELL(익절)이 슬리피지 버퍼(기본 3%) 적용 후 진입가 이하가 되면 차단되고 정규장까지 대기한다. 손절 판정은 `event.reason` 에 '손절' 문자열이 있는지로만 한다.
  - main:src/core/engine.py:1855-1872 (:1859 `is_stop_loss = bool(event.reason and "손절" in event.reason)`)
- **M-G5** (high) SELL pending 인 종목은 유령 정리에서 보호된다(30분 초과 또는 좀비 후보 마킹이면 강제 제거). exit_exempt 종목은 KIS 응답 누락이 3주기 연속일 때만 제거한다.
  - main:src/schedulers/kr_scheduler.py:1339-1352 (30분/1800초), :1326-1336 (면제 3회), :1286 (부분 누락 재시도에서 SELL pending 제외)
- **M-G6** (high) `_sell_blocked_symbols`(청산 실패 블랙리스트)는 main 에 읽기·삭제만 있고 **추가하는 writer 가 한 곳도 없다** — 사실상 죽은 게이트다.
  - `git grep -n "_sell_blocked_symbols" origin/main -- src scripts` 결과 6곳 전부 초기화(scripts/run_trader.py:199)·읽기(kr_scheduler.py:1081-1082)·삭제(:1085,:1088,:1364,:5299)
- **M-S1** (high) 세션 경계에서 main 은 '재준비'를 하지 않는다 — 일자 전환 시 전일 미체결을 전부 취소 시도하고 브로커 인메모리 장부·엔진 pending·exit pending 을 **비운다**. 다음 날 보호 SELL 은 생산자(시세 틱 → `update_price`)가 처음부터 다시 만든다. 재준비 횟수 상한이라는 개념 자체가 없다.
  - main:src/schedulers/kr_scheduler.py:5240-5265 (:5243-5250 전일 미체결 취소, :5254-5256 브로커 dict clear, :5259-5265 pending clear)
- **M-S2** (high) closing 처리는 '지정가 강등'이다: KIS 브로커가 세션을 보고 `pre_close`/`closing` 에서 시장가를 거부하고 지정가만 받는다. 15:30~15:40(`break`)과 `closed` 는 전면 거부. 즉 main 은 15:40 을 기다리지 않고 동시호가에 지정가로 남긴다.
  - main:src/execution/broker/kis_kr.py:484-494 (:487-488 break 거부, :491-494 동시호가 시장가 거부)
- **M-S3** (high) 모든 KR 주문은 `submit_order` 머리에서 킬스위치 검사와 감사 원장(EV_SUBMIT/EV_REJECT, 차단 시 record_blocked)을 통과한다 — 보호 SELL 도 예외가 아니다.
  - main:src/execution/broker/kis_kr.py:451-478, :560-565
- **M-S4** (high) SELL 체결이 오면 `run_fill_check` 가 exit pending 3종을 즉시 지우고 `exit_manager.on_fill` 로 `remaining_quantity`·stage 승격을 갱신한다(30초 sync 의존 제거).
  - main:src/schedulers/kr_scheduler.py:2808-2819
- **D1** (high) engine 브랜치(HEAD 1a2cb4d)의 legacy 는 merge-base 93c2fbd 시점이라 PR #81·#83·#84 의 기계장치가 **통째로 없다**: `_SellKeep`·`_pending_cancel_keep`·`stale_order_still_live`·`_keep_stale_sell_after_failed_cancel`·`on_heartbeat`·`release_kept_stale_buy`·`_is_exit_exempt` 중앙 가드·`_exempt_cancel_last_try` 가 전부 부재한다.
  - `git merge-base origin/main HEAD` = 93c2fbd; `git diff origin/main HEAD -- src/core/engine.py` 의 삭제 훅 (engine.diff:349-350, 373-376, 385, 395, 448, 623-630, 801, 939, 988, 1004, 1062) — engine 브랜치 src/core/engine.py 에는 해당 심볼이 grep 되지 않는다
- **D2** (high) S4-0 특성화 중 main 에서 **확실히 뒤집히는 것은 2건**이다. ① `test_in_the_closing_auction_the_cancel_is_sent_and_nothing_is_reordered` — main 은 동시호가에서 취소를 **보내지 않으므로** `kinds(broker) == ['cancel']` 단언이 깨진다. ② `test_an_exit_exempt_symbol_is_still_market_sold_by_the_fallback_loop` — main 은 면제 분기가 먼저라 `['cancel','submit']` 이 아니라 취소만 나가고 pending 이 해제된다.
  - tests/test_engine_legacy_stale_eviction_characterization.py:181-193, :240-250 (engine 브랜치) vs main:src/core/engine.py:2297-2303, :2259-2288
- **D3** (high) `test_a_stale_buy_is_released_whether_the_cancel_matched_anything_or_not`(취소 0건도 ACK 와 똑같이 읽는다)는 **부분적으로** 뒤집힌다 — main 도 최종적으로 해제하지만 경로가 `stale_order_still_live` 를 거치고, 0건 해제에는 `block_symbol`(BUY 5분 재매수 차단)이라는 새 부작용이 붙었으며 판단 불가 15회(`_MAX_BUY_KEEP`) 상한이 생겼다. 단언 자체는 통과할 수 있으나 '같게 읽는다'는 서술은 더 이상 참이 아니다.
  - tests/test_engine_legacy_stale_eviction_characterization.py:272-287 vs main:src/core/engine.py:1763 (`_MAX_BUY_KEEP = 15`), :1780-1812, :1804-1807 (`self.block_symbol(s)`)
- **D4** (high) `test_nothing_is_swept_while_no_signal_arrives` 의 docstring 주장('두 루프는 on_signal 본문 안에만 있다')은 main 에서 거짓이 된다 — `on_heartbeat` 가 **유지 중인 SELL 에 한해** SIGNAL 없이 재시도를 구동한다. 다만 최초 90초 감지와 10분 BUY 정리는 main 에서도 여전히 SIGNAL 구동이다.
  - tests/test_engine_legacy_stale_eviction_characterization.py:253-267 vs main:src/core/engine.py:2507-2516 (`if not self._pending_cancel_keep: return None`)
- **D5** (high) S4-0 이 고정한 '현행 결함' 중 **main 에서도 그대로인 것이 3건**이다: ① 폴백 상한 뒤 취소 미송신(원 지정가 방치), ② `submit_order` 예외 시 접수 여부를 모른 채 `clear_pending`, ③ 한 배치에서 고점수 BUY 2건이 서로 다른 희생자를 연쇄 축출(전역 상한 없음 — engine 브랜치는 attach 에만 H10 전역 쿨다운을 넣었고 legacy 는 불변).
  - main:src/core/engine.py:2291-2296 (①), :2378-2380 (②), :1435-1545 에 전역 쿨다운 없음(③, 종목별 `_REPLACEMENT_COOLDOWN_SEC` 만) — 대응 시험 tests/test_engine_legacy_stale_eviction_characterization.py:169, :215, :378
- **A1** (high) M1 후보 ①: main 은 **전량 청산 SELL 에 대해 '취소 0건 = 소멸'을 그대로 유지**하며 살아 있을 수 있는 지정가 위에 시장가를 얹는다. 근거 문장은 매도가능수량에 대한 가정이다 — "전량 매도(손절·트레일링)는 원 주문이 살아 있으면 KIS 가 주문가능수량 0 으로 거절하므로 과매도가 불가능하다".
  - main:src/core/engine.py:2322-2328 (인용은 :2324-2325), 같은 가정이 main:src/schedulers/kr_scheduler.py:988-990 ("전량 청산(손절·트레일링·EOD, 수량상 전량인 익절)은 재발행돼도 KIS 가 주문가능수량 초과로 거절하므로 종전대로 즉시 해제")
- **A2** (high) M1 후보 ②: 3분류의 1차 판정이 **브로커 인메모리 캐시**(`get_open_orders`)라 재시작 직후에는 거래소 조회 없이 항상 False(소멸)가 된다. kis_kr 스스로 그 캐시를 "⚠️ 재시작 후 비어 있음"이라고 경고한다.
  - main:src/core/engine.py:2394-2398 (`if not any(... await broker.get_open_orders()): return False`) + main:src/execution/broker/kis_kr.py:1115-1118 ("⚠️ 인메모리 캐시 — 재시작 후 비어 있음, 실 조회는 get_exchange_open_orders 사용")
- **A3** (high) M1 후보 ③: 면제 SELL 의 생존 판정 두 곳도 같은 인메모리 캐시만 본다 — 거래소 실 조회로 승격되지 않는다.
  - main:src/core/engine.py:2274-2277 · main:src/schedulers/kr_scheduler.py:843-850
- **A4** (high) M1 후보 ④: `cancel_all_for_symbol` 자체가 `self._pending_orders`(인메모리)만 순회하므로 재시작 후에는 '취소 대상 0건'이 되어 거래소에 살아 있는 주문에 취소 POST 가 한 번도 나가지 않는다.
  - main:src/execution/broker/kis_kr.py:932-949 (:939-942)
- **A5** (high) M1 후보 ⑤: '소멸' 판정의 명시 근거 문장이 코드 주석으로 남아 있다 — "브로커가 그 종목의 활성 주문을 더는 추적하지 않으면 소멸 — 해제한다(2026-08-04 P1 유지)". 즉 취소 최종성이 아니라 **브로커 장부의 부재**를 최종성으로 읽는다.
  - main:src/core/engine.py:2386-2387
- **A6** (high) 거래소 실 미체결 조회는 첫 페이지만 읽으며, 물은 종목이 첫 페이지에 없고 `tr_cont` 가 F/M 이면 '미체결 없음'이라 말하지 않고 None(판단 불가)을 돌린다 — 여기는 fail-closed 다.
  - main:src/execution/broker/kis_kr.py:1103-1109
- **A7** (high) attach 쪽에는 대응물이 아예 없다: 보호 전이 계산 함수 `quote_protection` 은 있으나 제품 생산자(시세를 넣는 주기 호출자)가 0건이고, `monitor_positions`·`_check_exit_signal` 은 attach 에서 전면 skip 이다(차단 사유 21). 따라서 P1 은 '동등화'가 아니라 '생산자 신설 + 동등화'다.
  - src/execution/safety/protection.py:431-461 (함수), 호출자는 src/execution/safety/runtime.py:1237 과 protection_recovery.py:426 뿐(둘 다 safety 내부) · docs/operations/claude-migration-handoff-2026-09-20.md:42 (차단 사유 21)
- **A8** (high) '취소 0건 = 소멸' 가정이 **없는** 곳도 명확히 있다: 분할 매도 경로(엔진 keep 관문·스케줄러 keep 관문)와 면제 SELL 경로는 0건을 소멸로 읽지 않고 유지·재시도한다. 즉 main 의 가정은 경로별로 분기돼 있고 일괄된 정책이 아니다.
  - main:src/core/engine.py:2449-2505 (분할), :2257-2288 (면제) · main:src/schedulers/kr_scheduler.py:983-1048 (분할)

**checklist**

- [생산자] attach 에 보호 청산 구동기가 존재한다 — 최소한 main 의 20초 REST 폴링과 30분 배치에 대응하는 주기 생산자가 owner 의 quote 경로를 부르고, owner 게시본을 읽어 SELL 후보를 emit 한다 (main 대응: kr_scheduler.py:4867+4996, batch_analyzer.py:1731-1874)
- [생산자] 그 생산자가 market_data(ma5·prev_low·high·low)를 붙인다 — 붙이지 않으면 복합 트레일링이 죽는다. main 의 WS 경로 비대칭(M-P2)은 **복제하지 않는다**(동등의 하한은 REST 경로다)
- [발화] 손절·트레일링(2갈래+본전이탈)·분할익절 3단계·복합트레일링·보유기간·횡보·익절후저효율·추세무효화 8종이 attach 에서 발화하고, 판정 순서가 main 과 같다(보유기간 계열이 손절보다 앞)
- [발화] 갭EOD(gap_and_go, 15:10, 손익<0%)·테마EOD(theme_chasing, 15:10, 손익<+1%)가 attach 에서도 전량 SELL 을 낸다 — 이 둘은 ExitManager 밖 시각 게이트라 별도 이식이 필요하다
- [발화] RSI2 청산·batch 보유기간(달력일) 두 경로를 attach 에서 살릴지 **명시 결정**한다 — 살린다면 ExitManager 영업일 기준과의 이중 기준(M-E5)을 그대로 옮길지 정한다
- [취소 3분류] 취소 뒤 판정이 소멸/생존/판단 불가 3값이고, 1차 판정이 재시작에 취약한 인메모리 장부가 **아니다**(A2·A4 를 복제하지 않는다 — attach 의 동등 기준은 '거래소 조회를 정본으로'다)
- [취소 3분류] 판단 불가의 상한이 횟수가 아니라 시간(180초, 등록 후 총 270초)이고, 생존 확인된 주문은 상한 없이 유지하며 시장가를 얹지 않는다
- [취소 3분류] 재시도 간격이 직전 판정에 따라 20초/60초로 갈리고, 재시도가 SIGNAL 없이도 구동된다(주기 task 또는 하트비트 등가물), 한 주기의 처리 시간에 상한이 있다
- [전량 vs 분할] 전량 청산은 즉시 폴백(지연 없음), 분할은 keep 관문 — 이 분기가 **등록 시점 의도**로 결정되고 나중의 잔고 스냅샷 비교로 추정하지 않는다
- [전량 vs 분할] 전량 청산의 즉시 폴백이 '매도가능수량 0 거절'이라는 팩트에 기대지 **않는** 근거를 갖는다(A1 은 main 의 가정이고 제약상 attach 에서 만들 수 없다) — 대안은 '취소 ACK 확인 후 폴백' 또는 '생존 확인 시 유지'
- [상한] 시장가 폴백 2회 상한, 상한 뒤 처리가 명시돼 있다 — main 의 '취소조차 보내지 않고 해제'(D5-①)는 결함이므로 **동등 대상이 아니다**
- [세션] 동시호가(15:20~15:30)에서 취소를 보내기 전에 분기해 원 지정가를 남긴다(M-C6). 15:30~15:40 전면 거부, closing 은 지정가 강등(M-S2)
- [세션] 세션 경계 재준비의 소유자가 정해져 있다 — main 은 재준비 없이 '전량 취소 + 장부 비우기 + 다음 날 생산자가 재생성'이다(M-S1). attach 가 재준비를 한다면 횟수 상한과 실패 시 처분이 함께 명세된다
- [F8①] 같은 종목에 미체결 BUY 가 있을 때 보호 SELL 이 막히지 않는다 — main 의 등가물은 `release_kept_stale_buy`(청산 검사 직전 호출)와 stale BUY 의 '부분체결 보유 중이면 청산 우선 해제'다
- [late-fill] '취소 0건의 가장 흔한 원인은 방금 체결'이라는 대비가 있다 — 첫 회는 조회 없이 한 주기 기다리고, 그 사이 체결 증거가 오면 pending 이 사라진다(main:engine.py:2454-2456 의 계약)
- [side 인지] 생존 판정이 같은 종목의 **반대 방향** 주문을 생존 근거로 읽지 않는다(main:engine.py:2396-2398)
- [면제] exit_exempt 가드가 attach 의 모든 SELL 경로에 있다 — 최소 main 의 9곳(M-G1)에 대응하는 지점, 특히 '면제 등록 전에 나간 SELL 은 취소만 하고 재주문 없음' + '취소 미확인이면 pending 유지·60초 재시도'
- [면제] await 중 면제가 등록되는 경합을 두 곳에서 재검사한다(폴백 머리 + 제출 직전)
- [래치] 래치·durable 접수 행의 해제가 **결정의 소비자가 있는 상태에서만** 일어난다(차단 23) — 해제가 아무도 받지 않는 보호 결정을 커밋해 pending_stage 를 영구히 세우지 않는다
- [킬스위치/원장] 보호 SELL 이 킬스위치와 감사 원장을 통과한다(P0-1 로 닫힘 — 회귀 없음 확인)
- [교착 관측] 유지 중인 SELL 이 헬스 모니터/하트비트에서 보인다 — pending 등록 시각을 되감아 교착 경보를 가리지 않는다(main:engine.py:1274-1277 의 명시 결정)
- [미설치 경로] legacy(미설치) 경로가 바이트 동일 — S4-0 특성화 26건 중 뒤집히는 2건(D2)은 조용히 고치지 않고 '결정으로 기록' 후 시험을 main 기준으로 갱신한다

**open_questions**

- S4-0 특성화 26건의 처분: main 을 engine 에 들이는 날 D2 의 2건을 (a) main 기준으로 재작성할지 (b) legacy 를 main 과 동기화하고 시험을 그대로 둘지. 후자는 '미설치 경로 바이트 동일' 제약과 충돌한다(legacy 를 바꾸는 것이므로) — 사용자/코디네이터 결정 필요.
- P1 의 '동등' 기준선은 main 의 **현재 동작**인가, main 의 **의도된 동작**인가. D5 의 3건(상한 뒤 취소 미송신·submit 예외 시 clear_pending·축출 연쇄)은 main 에 남아 있는 결함이다 — attach 가 그대로 복제하면 '동등'이지만 더 나쁘게 만든다.
- A1 의 회피책: 전량 청산의 즉시 폴백이 '매도가능수량 0 거절'에 기대지 않으려면 (a) 취소 ACK 를 확인한 뒤에만 폴백(손절 지연 발생) (b) 생존 확인 시 유지(손절이 영영 안 나갈 수 있음) (c) 취소 없이 시장가만(과매도 위험) 중 어느 쪽인가. main 은 (c) 에 가깝고 그 안전성을 매도가능수량 가정으로 정당화한다.
- A2/A4 를 attach 에서 고치면 취소·조회 API 호출량이 늘어난다 — KIS 원장 TR 계좌당 초당 1건(EGW00215) 한도 안에서 20초/60초 재시도 + 보호 quote 생산자 + P0-2 reconciler 가 공존 가능한가. 예산 배분이 P1 설계의 제약인지 확인 필요.
- attach 의 보호 quote 생산자는 WS 틱을 받을 것인가, REST 20초만으로 시작할 것인가. WS 를 받으면 차단 23(접수 행 해제 경로 없음)이 틱마다 터질 위험이 있고, REST 20초만이면 main 의 WS 실시간 손절보다 느리다 — '동등'의 판정 기준이 애매하다.
- 차단 22(부분 체결 주문의 장 마감 소멸)가 P1 범위 안인가 밖인가. 보호 SELL 이 부분 체결된 채 마감되면 attempt 가 영구 partial → 그 종목 잠금 + 일자 전환 영구 BLOCKED 이므로, P1 의 세션 경계 처리와 사실상 같은 문제를 건드린다.
- `_sell_blocked_symbols`(M-G6) 가 main 에서 죽은 게이트인데, attach 로 옮길 때 되살릴 것인가 삭제할 것인가. '청산 실패 블랙리스트'라는 의도는 attach 의 blocked_unknown 과 개념이 겹친다.
- M-E5 의 이중 보유기간 기준(영업일 vs 달력일)을 attach 에서 통일할 것인가. 통일은 '게이트를 여는/닫는 변경'이므로 어느 쪽이든 먼저 결함 이름을 붙여야 한다.

#### 조사 C(생산자 입력)

- **L1** (high) legacy 가 보호 판단에 쓰는 시세 생산자는 3개다 — (a) REST 폴링 20초 주기 `run_rest_price_feed`, (b) KIS WS 콜백 `_on_market_data`, (c) 30분 주기 `monitor_positions`. 세 경로 모두 최종적으로 `ExitManager.update_price(symbol, price, market_data)` 하나로 수렴한다.
  - src/schedulers/kr_scheduler.py:4903-4908(docstring "보유종목: 20초 주기, 모든 세션")·:4940-4996(세션별 get_quote/get_overtime_quote)·:5008(`await self._check_exit_signal(symbol, Decimal(str(price)), market_data=_md)`); scripts/run_trader.py:1923-1932(WS 콜백 → `_check_exit_signal(event.symbol, event.close)`, market_data 없음); src/core/batch_analyzer.py:13("[매 30분] monitor_positions()")·:1783-1839
- **L2** (high) REST 경로의 시세에는 **거래소 원시각 필드가 없다**. `get_quote` 는 TR `FHKST01010100` 의 output 에서 price/open/high/low/prev_close/volume/change/change_pct 만 뽑고 체결시각 필드를 읽지 않는다. 따라서 legacy 의 REST 기반 보호 판단은 '수신 시각 = 시장 시각'으로 암묵 가정한다.
  - src/execution/broker/kis_kr.py:1630-1667(`tr_id="FHKST01010100"`, 반환 dict 에 시각 키 0개)·:1673-1722(`get_overtime_quote` FHPST02300000 도 동일, 체결가 없으면 호가 mid 로 폴백)
- **L3** (high) WS 경로에만 원시각과 사건 신원이 있다. `MarketObservation` 이 raw_date+raw_time(KST)에서 `market_as_of` 를, `connection_id:frame_sequence:record_index` 에서 `source_event_id` 를 파생하고, `source` 는 `kis_websocket:<tr_id>`(H0STCNT0=KRX, H0NXCNT0=NXT)로 고정된다. `received_at` 은 시장 시각과 분리 보관된다.
  - src/core/market_observation.py:20-28(docstring)·:52-56(_TR_EXCHANGES)·:95-101(market_as_of/source_event_id 파생)·:133-158(_derive_market_as_of, tzinfo=KST); src/data/feeds/kis_websocket.py:866-904(feed 가 MarketObservation 을 만들어 MarketDataEvent.observation 에 싣는다)
- **L4** (high) legacy 의 보호 판단 입력 `market_data` 는 4키다 — ma5·prev_low(pykrx 일봉에서 **일 1회** 캐시)와 high·low(그 틱의 quote). WS 콜백 경로는 market_data 를 아예 넘기지 않아 복합 트레일링(MA5·전일저가)이 그 틱에서는 죽는다.
  - src/core/batch_analyzer.py:1502-1548(`_refresh_composite_cache` — pykrx `get_market_ohlcv_by_date`, `if self._composite_cache_date == today: return`)·:1831-1838(_md 4키); src/schedulers/kr_scheduler.py:4999-5005(같은 4키, prev_low 는 `self._prev_day_low`); scripts/run_trader.py:1932(WS 는 market_data 인자 없음); src/strategies/exit_manager.py:1194-1195(`if market_data is None: return None`)
- **L5** (high) attach 에서 legacy 시세가 보호 경로로 들어가는 입구는 **네 곳 모두 닫혀 있다**: `monitor_positions`·`_check_exit_signal`·`_cleanup_stale_pending` 은 attach 를 감지하면 즉시 return 하고, `engine.update_position_price` 는 `ApplicationBlocked` 를 올린다.
  - src/core/batch_analyzer.py:1757-1759; src/schedulers/kr_scheduler.py:1031-1033·:927-928; src/core/engine.py:945-947("가격 보호 변경은 execution.quote로 직렬화해야 합니다")
- **L6** (high) **새 결함(표 16~23 에 이름이 없다)**: attach 에서도 `StrategyManager.on_market_data` 가 계속 등록돼 있고 그 첫 줄이 `engine.update_position_price` 다. MARKET_DATA 는 attach 의 legacy 이벤트 거부 목록(FILL/ORDER/SIGNAL)에 없어 핸들러까지 도달하므로, **시세 틱마다** ApplicationBlocked 가 나고 핸들러 루프가 그것을 삼켜 `errors_count`+ErrorEvent 만 남는다 — attach 에서는 시세 기반 전략 평가(매수 신호 포함)가 통째로 죽는다. factory·runtime 어디에도 핸들러를 해제하는 코드가 없다.
  - src/core/engine.py:1281(`engine.register_handler(EventType.MARKET_DATA, self.on_market_data)`)·:1301-1304(핸들러 첫 줄이 update_position_price)·:541-547(거부 목록은 FILL/ORDER/SIGNAL 뿐)·:635-653(핸들러 예외를 흡수해 ErrorEvent 로 바꾼다); `grep -n "register_handler|_handlers|strategy_manager" src/execution/safety/factory.py src/execution/safety/runtime.py` 결과 0건
- **Q1** (high) owner 의 시세 입구는 두 개다. `observe_market(event, *, intent_id=None, market_data=None)` 는 **WS MarketDataEvent 전용**이고(파서가 `type(event) is MarketDataEvent`·`type(event.observation) is MarketObservation`·`source=='kis_websocket'` 을 요구), 내부에서 `quote(...)` 를 `entry_observation=observation` 으로 호출한다. REST 시세는 `quote()` 직접 호출밖에 없다.
  - src/execution/safety/runtime.py:1152-1158; src/execution/safety/market_source.py:81-95(`observation_from_event` — `market_observation_required`·`market_event_observation_mismatch`)
- **Q2** (high) `quote()` 의 입력 계약: symbol(비공백 str)·price(양의 유한 Decimal) 필수, 선택 5개 `market_data`(dict|None)·`intent_id`·`market_as_of`(aware datetime)·`source`·`source_event_id`·`entry_observation`. market_as_of/source/source_event_id 는 **셋 중 하나라도 주면 셋 다** aware/비공백이어야 하고 `market_as_of > now` 면 `future_market_quote`. 셋 다 None 이면 '시각 없는 보호 전용 입력'이 된다.
  - src/execution/safety/runtime.py:1160-1180
- **Q3** (high) owner 가 durable 하게 남기는 시세 행은 4종이다 — `protection_quote_admissions[command_id]`(요청 전문+payload_digest+status RECEIVED+source_version+admitted_at), `quote_price_views[symbol]`(price·source_version·received_at·market_as_of·source·source_event_id 6키 고정), `latest_explicit_quote[symbol]`(market_as_of 가 있을 때만; price·as_of·source·source_event_id·market_data·received_at·admission_version·payload_digest 8키), `market_sources[symbol]`(entry_observation 이 있을 때만; request 전문·digest·admission/completed/invalidated version·decision).
  - src/execution/safety/runtime.py:1208-1221(admit)·:1262-1265(reduce); src/execution/safety/market_source.py:41-45(quote_price_view)·:47-78(validate_price_views 의 키 집합·부호·시각 규칙)·:120-131(complete_source); src/execution/safety/runtime.py:418-443(_validate_explicit_quotes 8키·digest)
- **Q4** (high) stale 규칙은 **종목별 시각 단조성** 하나다: `_require_quote_freshness` 가 floor=max(당일 평가 스냅샷의 as_of, latest_explicit_quote 의 as_of)보다 이른 `market_as_of` 를 `stale_market_quote` 로 거부한다. **market_as_of 가 None 이면 검사 자체를 건너뛴다** — 즉 REST 시세는 stale 보호를 전혀 받지 않는다.
  - src/execution/safety/runtime.py:444-450(_quote_time_floor)·:451-457(`if observed is None: return`)
- **Q5** (high) 중복/충돌 규칙은 두 겹이다: (a) 같은 (source, source_event_id) 인데 payload digest 가 다르면 `market_quote_event_conflict`, (b) entry_observation 있는 경로에서 같은 event_id 의 완전 동일 요청은 `completed_duplicate` 가 저장된 decision 을 그대로 재사용하고(멱등), request 가 observed_at 만 빼고 다르면 `market_source_event_conflict`.
  - src/execution/safety/runtime.py:457-461·:413-417(_market_quote_payload — intent/수신시각은 동일성에서 제외); src/execution/safety/market_source.py:215-228(completed_duplicate)
- **Q6** (high) 미해결 행 규칙: 같은 종목에 `RECEIVED` 접수 행이 하나라도 있으면 다음 `quote()` 는 admit 단계에서 거부되고, 접수 행은 **정상 reduce commit 에서만** 지워진다. 그리고 `market_source_pending` 이 True 인 동안(= 진행 중 보호 task 1건 or 접수 행 1건 or `_protection_failed` 래치) **전 종목의 모든 SUBMIT 이 막힌다** — 보호 SELL 도 포함이다.
  - src/execution/safety/runtime.py:1209-1210·:1266·:463-466; src/execution/safety/commands.py:363(`_require(not self.runtime.market_source_pending(state), 'market_source_pending')` — side 무관)
- **Q7** (high) 시세 입력은 **전 종목 직렬**이다. `apply_quote` 전체가 단일 `self._quote_lock` 안에서 돌고 한 건당 owner mutate 2회(admit·reduce = durable 쓰기 2회)를 한다. 보유 8종목을 20초마다 갱신하면 그 구간 대부분에서 `market_source_pending` 이 참이 된다.
  - src/execution/safety/runtime.py:95(`self._quote_lock = asyncio.Lock()`)·:1270-1292
- **Q8** (high) **치명적 입력 규칙**: 분할익절 pending 이 새로 생기는 순간 `intent_id` 가 None 이면 `quote_protection` 이 ValueError 를 던지고, 그 예외는 **admit commit 뒤의 reduce** 에서 나므로 (a) 접수 행이 영구히 남고 (b) `_protection_failed` 가 래치된다 → 차단 사유 23 의 영구 봉쇄가 즉시 발생한다. 생산자는 pending 을 만들 수 있는 모든 틱에 intent_id 를 실어야 한다.
  - src/execution/safety/protection.py:455-460("신규 pending 보호 목표에는 intent_id가 필요합니다"); src/execution/safety/runtime.py:1229-1266(reduce 안에서 호출)·:1307-1311(콜백이 admission_rejected 가 아닌 예외를 `_protection_failed=True` 로 래치)·:1266(접수 행 삭제는 reduce 성공 시에만)
- **Q9** (high) 그 `intent_id` 는 아무 값이나 되는 것이 아니다. 보호 DTO 의 `pending_owners[symbol]` 로 저장되고, 복구 경로가 그것이 **state['intents'] 에 실재하는 SELL intent 이며 target_quantity == pending_target_qty** 임을 요구한다(`unknown_pending_owner`). 그런데 intents 는 gateway 의 prepare 시점에 `gw-i-<uuid>` 로 새로 만들어지므로, 생산자가 quote 에 넘긴 intent_id 와 gateway 가 쓰는 intent_id 는 **현재 구현상 반드시 어긋난다**.
  - src/execution/safety/protection.py:459-461(pending_owners 기록); src/execution/safety/protection_recovery.py:266-273(_pending_known)·:393(`unknown_pending_owner`); src/execution/safety/lifecycle.py:283-285(intents 생성은 prepare); src/execution/safety/gateway.py:151-156(`self._intents[key] = 'gw-i-'+uuid4().hex`)
- **Q10** (high) intent 불일치의 두 번째 피해: 체결이 와도 `owns_pending` 이 거짓이라 `on_fill` 의 pending 전이가 복원되어 되돌려지고 stage 가 올라가지 않는다. 그리고 attach 의 `quote_protection` 은 **pending 이 있는 동안 pending_since 를 매 틱 now 로 고정**하므로 legacy 의 1800초/300초 하드 만료가 영원히 발화하지 않는다 → 그 종목의 보호는 손절 포함 **전부** 무기한 정지한다(`decision = None` 강제).
  - src/execution/safety/protection.py:410-420(saved_pending 복원 경로)·:447-457(`state.pending_since = now` 후 원필드 복원, `decision = None`); src/strategies/exit_manager.py:1280-1294(_hard_limit 1800/300 만료는 pending_since 경과가 있어야 발화)
- **Q11** (high) 보호 전용 입력(entry_observation 없음)은 그 종목의 **진입 증거를 무효화**한다(`invalidated_at_version`), 그러면 `source_is_current` 가 False 가 되어 그 종목의 BUY 는 `current_market_source_required` 로 거부된다. 즉 REST 기반 보호 생산자를 붙이면 그 종목의 자동 매수는 WS 원관측이 다시 들어오기 전까지 불가능하다.
  - src/execution/safety/runtime.py:1262-1264; src/execution/safety/market_source.py:196-213(source_is_current)·; src/execution/safety/commands.py:399-405(`current_market_source_required`·`current_entry_quote_required`)
- **Q12** (high) `observe_market` 경로에서 `market_data` 는 자유 dict 가 아니다. OHLC 계열 키를 주면 원관측 값과 같아야 하고(다르면 `market_data_observation_mismatch`), `low` 는 **항상 원관측의 당일 저가로 덮어쓰인다**. 따라서 복합 트레일링의 '당일저가'는 WS 관측이 소유하고, ma5·prev_low 만 생산자가 실어야 한다.
  - src/execution/safety/market_source.py:12-39(observation_market_data, 마지막 두 줄 `result['low'] = str(observation.low)`)
- **Q13** (high) 시각·일자 게이트가 추가로 두 개 있다. (a) `canonical_observation` 이 `market_as_of.date() == now.date()` 와 `market_as_of <= received_at <= now` 를 요구한다 → 일자 전환 직후·야간 세션의 관측은 그대로는 못 들어간다. (b) `quote()` 는 `_require_day_admission()` 을 통과해야 하므로 일자 전환이 PREPARED 이거나 미적용 inbox 행이 있으면 `day_transition_admission_closed` 로 시세 자체가 막힌다.
  - src/execution/safety/market_source.py:96-113(canonical_observation); src/execution/safety/runtime.py:1164(`self._require_day_admission()`)·:148-159(day_admission_closed 의 4조건)
- **D1** (high) 생산자가 호출해야 하는 것: 정규장·WS 커버 종목은 `runtime.observe_market(event, intent_id=..., market_data={'ma5':…, 'prev_low':…})`, WS 미커버·시간외는 `runtime.quote(symbol, Decimal(price), market_as_of=None, source=None, source_event_id=None, intent_id=..., market_data=…)`. 받는 것은 `None | (action, quantity, reason)` 3-튜플이고 action 은 'sell_all'|'sell_partial' 이다(outbox 행도 같이 생기지만 **소비자가 0건**이다).
  - src/execution/safety/runtime.py:1152-1158·:1236-1252(decision → outbox `protection_decision` status pending); src/execution/safety/protection.py:441-461(quote_protection 반환); `grep -rn protection_decision src/` 결과 소비자는 journal_delivery.py:200·221 의 **제외 필터**뿐
- **D2** (high) SUBMIT 으로 가는 유일한 길은 SignalEvent 다 — `engine.emit(SignalEvent)` → `_submit_signal` → `risk_manager.on_signal` → `gateway.submit`. 따라서 생산자는 decision 3-튜플을 legacy 와 같은 모양의 SELL Signal(price=그 틱 가격, metadata {'source','quantity','exit_action'}, reason=decision[2])로 바꿔 emit 해야 하고, reason 문자열이 그대로 `exit_type` 태그가 된다.
  - src/core/engine.py:536-539·:660-672; src/execution/safety/gateway.py:44-72·:90-96(`classify_exit_type(reason)`); src/utils/exit_types.py:9-60; 대조 legacy 모양 src/schedulers/kr_scheduler.py:1162-1194
- **D3** (high) 잠금·예외 순서: quote 는 `asyncio.shield(task)` 로 호출자 취소와 분리되고, admit 전 사전 거부(3종)는 **래치하지 않지만** 호출자에게 예외로 올라간다(`stale_market_quote`·`market_quote_event_conflict`·`미해결 보호 가격 입력…`). admit 후 실패는 전부 `_protection_failed` 영구 래치다. 생산자는 이 두 부류를 반드시 다르게 다뤄야 한다(전자는 그 틱만 버리고 계속, 후자는 프로세스 정지 신호).
  - src/execution/safety/runtime.py:1197(사전 검사)·:1211-1218(ApplicationBlocked 에서 admission_rejected=True 후 재raise)·:1300-1312(shield·done_callback 래치)·:1272-1280(entry 경로의 사전 재검사도 admission_rejected 로 분류)
- **D4** (high) owner 는 시세를 받으면 live 객체에 세 가지를 투영한다 — `engine.portfolio.positions[*].current_price`(=_view_price), owner state 의 `positions[symbol]['highest_price']`, 그리고 `publish_protection` 으로 **live ExitManager 의 config/_states/_entry_times/_exit_exempt 전체**. 즉 attach 에서 exit_manager 는 owner 의 읽기 전용 게시 대상이고, 생산자가 exit_manager 를 직접 만지면 안 된다.
  - src/execution/safety/runtime.py:1244(highest_price)·:1282-1289(_quotes/_quote_metadata·current_price)·:510(`publish_protection(self.exit_manager, state["protection"], clock=self.clock)`); src/execution/safety/protection.py:299-310(publish_protection — `_exit_exempt` 는 같은 set 객체를 유지)
- **E1** (high) **ExitManager 안에 있는 것**(= owner quote 경로가 그대로 물려받는 것): 보유기간 초과(영업일), 횡보 청산, 익절후 저효율, 추세 무효화(신고가 실패), 손절(ATR/레짐/급락 cap 포함), 1·2·3차 분할익절, 본전보호(breakeven), ATR/고정 트레일링, 복합 트레일링(MA5·전일저가), 고점 갱신.
  - src/strategies/exit_manager.py:927-1180(update_price 본문)·:1181-1254(_check_composite_trailing)·:1255-1366(_check_partial_exit)
- **E2** (high) **스케줄러/배치 안에만 있는 것**(= owner 가 물려받지 못해 생산자나 별도 owner 가 다시 만들어야 하는 것): 갭EOD(gap_and_go 15:10 이후 손실 포지션 전량), 테마EOD(theme_chasing 15:10 이후 +1% 미만), RSI2 청산(FDR 일봉 RSI(2)>70), 보유기간 초과의 **달력일 판정 중복분**, 코어홀딩 조기경보(-12%·MA200 3일). 이들은 `update_price` 를 부르지 않고 직접 Signal 을 만든다.
  - src/schedulers/kr_scheduler.py:1095-1117(테마EOD)·:1119-1151(갭EOD); src/core/batch_analyzer.py:1798-1824(RSI2)·:1869-1893(`holding_days = (datetime.now() - pos.entry_time).days`)·:1903-1941(_monitor_core_positions)
- **E3** (high) 예외적으로 **급락 선제 stale 청산만은 이미 owner 안에 있다** — `stale_exit_candidate.select_preemptive_stale` 이 IntradayRiskOwner 의 reducer 안에서 후보를 골라 outbox 에 `protection_decision`(effect_source='intraday_preemptive') 를 만든다. 그러나 이것도 소비자가 없고, legacy 쪽 원본 `_preemptive_stale_exit_on_bear` 는 attach 에서 ApplicationBlocked 를 올린다.
  - src/execution/safety/intraday_owner.py:180-200; src/execution/safety/stale_exit_candidate.py:1-8(docstring "주문 신호, outbox, 효과 ID와 실행 권한은 호출자 소유"); src/core/batch_analyzer.py:1595-1597
- **E4** (high) 보호 DTO 가 나르는 것은 `_STATE_FIELDS` 39개(pending_stage·pending_since·pending_target_qty·pending_filled_qty·highest_price·last_new_high_date·breakeven_activated·actual_stop_pct 등)와 `_CONFIG_FIELDS` 33개, 그리고 execution 전용 3맵(`degraded`·`orders`·`pending_owners`)이다. 후보 계산은 `decode_protection` 이 파일 I/O 없는 복제 ExitManager 를 만들어 하고 `_pending_verifier` 는 항상 None 을 돌려주는 보수적 표식으로 고정된다(= 거래소 미체결 확인 없음).
  - src/execution/safety/protection.py:24-56(필드 목록)·:246-252(`_unknown_pending`)·:254-276(decode_protection)
- **T1** (high) 시험 corpus 의 정본 고정: `tests/test_execution_market_source.py` 가 **실제 WS feed 에 합성 프레임을 넣어** MarketDataEvent 를 만들고 `observe_market` 이 같은 commit 에서 entry+protection 을 완료함을 고정한다(`market_sources`·`entry_quotes`·`latest_explicit_quote`·접수 행 0). 같은 파일이 in-flight 시세가 prepare/dispatch 를 `market_source_pending`/NOT_SENT 로 막는 것도 고정한다.
  - tests/test_execution_market_source.py:80-95(market_event 헬퍼)·:98-128(same-commit 단언)·:18-77(in-flight SUBMIT 차단, boundary/operation 4조합)
- **T2** (high) quote/observe_market 를 부르는 시험은 15파일이며 밀도 상위는 day_recovery(22)·market_source(18)·market_source_review(16)·protection_recovery(11)·runtime(6). 제품 호출자는 여전히 0건이므로 이 corpus 가 입력 계약의 유일한 실행 증거다.
  - `grep -rn "\.quote(|observe_market(" tests/` 파일별 집계; docs/superpowers/plans/2026-09-22-p0-4-latch-reservation-session-stale.md:134(제품 호출자 0건 확인)
- **T3** (medium) 반대로 **없는 고정**이 세 종류다: ① `request_session_changed`·`request_session_mismatch`·`unsupported_submit_session` 을 단언하는 시험 0건 ② 보호 SELL 이 NOT_SENT 로 사라질 때의 관측 흔적 고정 0건 ③ quote 의 intent_id 누락이 접수 행+래치를 남긴다는 단언(내가 코드로 유도한 Q8)의 전용 시험도 확인하지 못했다(추측: 없음).
  - docs/superpowers/plans/2026-09-22-p0-4-latch-reservation-session-stale.md:179(F-A4-4 의 5갈래)·:427(S-C 범위)
- **S1** (high) 세션 경계 재준비의 소유자는 이미 **생산자**로 P0-4 가 확정했고(dispatch 안 세션 교체는 본문·fingerprint·증거 라벨을 동시에 거짓으로 만들어 금지), 재준비 횟수 상한은 미정이다 — legacy 는 `_pending_fallback_count` 2회 상한을 두지만 gateway 의 intent 재사용에는 상한이 없다. `closing`(15:20–15:30)에서 시장가는 build 단계 거부라 '지정가 강등 vs 15:40 대기'가 P1 결정으로 남아 있다.
  - docs/superpowers/plans/2026-09-22-p0-4-latch-reservation-session-stale.md:169-171·:214·:216·:347; src/execution/safety/requests.py:64-72(_session_at 6구간)·:105-109(request_session_mismatch)
- **S2** (high) KIS 유량 제약: 현재가 TR `FHKST01010100` 은 원장 TR 이 아니라 공용 10/s 슬라이딩 윈도우만 받는다(`_api_get` → `_rate_limit(tr_id)`). legacy REST 피드는 종목당 0.15초 sleep 으로 페이싱한다. WS 는 보유 종목을 priority 로 항상 구독하고 상한은 40종목이라 보유 8종목은 전량 커버된다.
  - src/utils/kis_rate_limit.py:25-26(MAX_RPS=10)·:31-33(LEDGER_TR_IDS 에 FHKST01010100 없음); src/execution/broker/kis_kr.py:262-266(`ledger_lease = await self._rate_limit(tr_id)`); src/schedulers/kr_scheduler.py:5014(`await asyncio.sleep(0.15)`); src/data/feeds/kis_websocket.py:101(MAX_SUBSCRIPTIONS=40)·:390-398(priority 우선 배정)
- **S3** (high) 시세 캐시는 세 군데로 흩어져 있다 — owner 의 durable `quote_price_views`/`latest_explicit_quote`(정본), runtime 의 메모리 `_quotes`/`_quote_metadata`(legacy checkpoint 호환용 폴백, 권위 아님), 그리고 legacy 의 pykrx 일봉 캐시 2벌(batch_analyzer `_ma5_cache`/`_prev_low_cache`, kr_scheduler `_ma5_cache`/`_prev_day_low`). 생산자는 마지막 것만 새로 소유하면 된다.
  - src/execution/safety/runtime.py:87-88·:396-403(메모리 cache 는 durable view 가 없을 때만); src/core/batch_analyzer.py:288-289; src/schedulers/kr_scheduler.py:302·:4822-4849

**checklist**

- 입력 1 — 출처: WS 커버 종목은 `observe_market(MarketDataEvent)` 로만, 미커버·시간외는 `quote(market_as_of=None, source=None, source_event_id=None)` 로만. 두 경로를 한 종목에 섞으면 진입 증거가 매 틱 무효화된다(Q11) → '오늘 이 종목의 시세 출처는 하나'를 생산자가 강제한다.
- 입력 2 — 시각: 시각 있는 입력은 종목별 `market_as_of` 단조 증가만 허용(floor = max(평가 스냅샷 as_of, 직전 explicit as_of)). 역행 프레임은 `stale_market_quote` 로 그 틱만 버린다(래치 아님).
- 입력 3 — 시각 없음의 명시: REST 입력은 market_as_of/source/source_event_id 를 **셋 다 None** 으로 보내고, 그 입력에는 stale 보호가 없다는 사실을 계약에 적는다(현재 legacy 와 같은 수준 — 더 약하지도 강하지도 않다).
- 입력 4 — market_data: {'ma5','prev_low'} 만 생산자 소유, 'low'/'high' 는 원관측이 소유(observe_market 경로는 자동 덮어쓰기). pykrx 일봉 캐시(일 1회 + 장중 신규 종목 즉시 채움)를 생산자가 가져간다.
- 입력 5 — intent_id: **모든 보유 종목 틱에 필수**(누락 = 접수 행 잔존 + `_protection_failed` 영구 래치, 차단 23 즉발). 값은 gateway 가 그 종목 SELL 에 쓸 intent_id 와 **같은 것**이어야 하고(`pending_owners` ↔ `state['intents']` 대조), target_quantity 일치까지 요구되므로 intent 발급 소유권을 생산자↔gateway 중 한쪽으로 옮기는 설계가 선행한다.
- 입력 6 — 예외 2분류: 사전 거부(stale/event_conflict/미해결 접수 행)는 '그 틱 폐기 후 계속', admit 이후 실패는 '프로세스 보호 정지'로 다르게 처리하고, 후자는 health 로 올린다.
- 입력 7 — 유량·직렬성: 전 종목 단일 `_quote_lock` + 건당 durable 쓰기 2회이고 그 동안 **전 종목 SUBMIT 이 막힌다**(`market_source_pending`). 틱 주기·종목 수 × 쓰기 지연이 보호 SELL 의 최대 지연이므로 주기 상한을 계약에 숫자로 넣는다.
- 입력 8 — 일자/세션 창: `_require_day_admission` 이 닫히는 구간(일자 전환 PREPARED·미적용 inbox·risk day 불일치)과 `market_as_of.date()==오늘` 요구 때문에 생산자는 '시세를 넣을 수 있는 시간대'를 자기 상태로 알아야 한다.
- 입력 9 — 결정의 소비자: decision 3-튜플과 outbox 행은 지금 아무도 읽지 않는다. 생산자가 곧 소비자(SELL SignalEvent 발행자)가 되며, reason 문자열이 그대로 `exit_type` 이 되므로 legacy 와 같은 문구 규약을 지킨다.
- 입력 10 — pending 중의 보호 정지: pending 이 있는 동안 attach 는 손절 포함 모든 결정을 억제하고 hard expiry 도 발화하지 않는다. pending 해제(체결·NOT_SENT·세션 소멸)의 소유자를 생산자 안에 명시하지 않으면 한 번의 분할익절이 그 종목 보호를 영구 정지시킨다.
- 입력 11 — 스케줄러에만 있던 청산(갭EOD·테마EOD·RSI2·코어 조기경보·달력일 보유기간)은 exit_manager 밖이다. P1 에서 '생산자가 다시 만든다 / 범위 밖으로 선언한다'를 항목별로 결정한다.
- 입력 12 — MARKET_DATA 핸들러: 생산자를 붙이기 전에 `StrategyManager.on_market_data` 의 `update_position_price` 가 attach 에서 매 틱 ApplicationBlocked 를 내는 것(L6)을 먼저 이름 붙이고 닫는다 — 안 그러면 새 생산자가 넣은 시세 틱이 동시에 전략 평가를 죽인다.

**open_questions**

- WS 원관측과 REST 시세를 한 종목에 섞을 수 없다면(Q11), WS 미커버 구간(프리장·넥스트장·WS 끊김)의 보유 종목은 보호를 받되 매수는 포기하는 것이 맞는가, 아니면 `quote()` 의 '보호 전용 입력이 진입 증거를 무효화한다' 규칙 자체를 P1 에서 손볼 것인가(= 게이트를 여는 변경이므로 막던 결함을 먼저 이름 붙여야 한다).
- pending SELL intent_id 의 발급 소유권: 생산자가 먼저 굽고 gateway 가 받아 쓰게 할 것인가(gateway._intents 주입), 아니면 quote 를 '결정 후 intent 바인딩' 2단계로 쪼갤 것인가. 후자는 quote 의 단일 commit 성질을 깬다.
- `market_source_pending` 이 SUBMIT 을 side 무관 전역으로 막는 현재 규칙에서, 20~30초 주기 생산자가 보호 SELL 자신의 발행 창을 얼마나 잡아먹는가 — 허용 가능한 주기·종목 수의 상한을 실측(스토어 쓰기 지연)으로 정해야 한다. 이것은 차단 사유 19 와 같은 계열의 정지 창이다.
- REST 시세에 stale 규칙이 전혀 없는 상태(Q4)를 그대로 둘 것인가. `market_as_of` 대신 수신 시각을 floor 로 쓰는 보수적 규칙을 넣으면 legacy 보다 엄격해지지만, KIS 가 시각을 주지 않으므로 '우리가 만든 사실'이 된다(D1·D9 의 금지선과 닿는다).
- pending 이 있는 동안 손절 결정까지 억제하는 현재 계약(Q10)을 P1 에서 유지할 것인가. main 은 pending 중에도 다음 틱에서 재감지해 같은 조건을 다시 낼 수 있다(kr_scheduler 의 `_exit_pending_symbols` 는 15분 stale 해제가 있다) — 동등성 목표와 직접 충돌한다.
- 갭EOD/테마EOD 는 시세가 아니라 '시각 + 미실현손익'만 보는 판단이다. 이것을 quote 생산자 안에 둘 것인가(시세 틱에 얹기), 아니면 별도 시각 기반 owner 명령으로 뺄 것인가 — 전자는 시세가 끊기면 EOD 청산도 같이 죽는다.
- `observe_market` 의 `market_as_of.date() == now.date()` 요구(Q13) 아래에서, 15:30 이후·일자 전환 PREPARED 이후의 보호 청산은 어떤 입구를 쓰는가. 현재 계약대로면 그 구간에는 시세를 넣을 방법 자체가 없다.
- L6(attach 에서 MARKET_DATA 핸들러가 매 틱 ApplicationBlocked)을 새 설치 차단 사유로 등록할 것인가, 아니면 P1 생산자 배선의 일부로 처리할 것인가 — 현재 표 16~23 에 이름이 없다.

## 2. 설계 초안(opus/high) — **§3 처분으로 바뀐 부분이 있다. 초안 그대로 구현하지 않는다**

> 기준 `1a2cb4d`(engine 브랜치 worktree). main 인용은 `origin/main`(`afa6e1e`) 기준이며 `main:` 접두를 붙인다. 아래 인용은 이 설계 작업에서 **직접 읽어 재확인**했다(조사 A/B/C 의 인용을 그대로 옮긴 것이 아니다). 재확인하지 못한 것은 그 자리에 "추측"이라고 적었다.

### 2-1. 조사가 확정한 사실 — 재확인 완료분

#### (가) attach 에는 보호 청산의 구동기도 소비자도 없다 (차단 21)

- **F1** `runtime.quote()`/`observe_market()` 의 **제품 호출자가 0건**이다. `grep -rn "\.quote(\|observe_market(" src/` 의 히트는 `src/execution/safety/runtime.py:1155`(자기 호출) 하나뿐이다.
- **F2** `quote()` 가 결정을 내면 `src/execution/safety/runtime.py:1246-1253` 이 outbox 에 `kind='protection_decision', status='pending'` 행을 쓰고 `:1300-1301` 이 3-튜플을 돌려준다. **그 행을 읽는 소비자는 0건**이다 — `grep -rn protection_decision src/` 의 히트 4곳은 생산(`runtime.py:1247`, `intraday_owner.py:193`), 교차검증(`market_source.py:190`), **제외 필터**(`journal_delivery.py:200-201`, `:221-222`)뿐이다.
- **F3** legacy 의 세 구동기가 attach 에서 전부 닫혀 있다: `src/core/batch_analyzer.py:1757-1759`(`monitor_positions` 조기 return), `src/schedulers/kr_scheduler.py:1031-1033`(`_check_exit_signal` 조기 return), `src/core/engine.py:945-947`(`update_position_price` → `ApplicationBlocked("가격 보호 변경은 execution.quote로 직렬화해야 합니다")`).
- **F4** legacy 의 보호 에스컬레이션(90초 SELL 시장가 폴백·10분 BUY 취소)은 `src/core/engine.py:1991` 이하의 `_pending_timestamps` 순회인데, attach 에서는 그 장부에 **아무도 쓰지 않는다**(`src/core/engine.py:2484-2490` 의 attach 분기가 legacy 장부 기록을 건너뛴다) — 오히려 남아 있으면 `:1983-1989`(H7)가 SIGNAL 을 거부한다. 즉 순회 대상이 구조적으로 0건이다.

#### (나) 새 결함 — attach 에서 시세 틱마다 전략 평가가 죽는다 (차단 24 신규, 표 16~23 에 이름이 없다)

- **F5** `StrategyManager` 는 attach 여부와 무관하게 `src/core/engine.py:1281` 에서 `EventType.MARKET_DATA` 핸들러로 등록되고, 그 핸들러의 **첫 줄**이 `:1304 self.engine.update_position_price(event.symbol, event.close)` 다. attach 의 legacy 이벤트 거부 목록은 `:541-547` 의 FILL/ORDER/SIGNAL 뿐이라 MARKET_DATA 는 핸들러까지 도달한다. 핸들러 루프는 예외를 `:646` 에서 흡수해 `errors_count`+ErrorEvent 로 바꾼다.
- **F6** MARKET_DATA 는 두 피드가 모두 낸다 — WS(`src/data/feeds/kis_websocket.py:892-903`, `MarketObservation` 동봉)와 REST 폴링(`src/schedulers/kr_scheduler.py:4983-4995`).
- ⇒ **attach 를 설치하면 시세 틱마다 `ApplicationBlocked` 가 나고 `on_market_data` 가 첫 줄에서 죽어, 그 뒤의 전략 평가(매수 신호 생성) 전체가 조용히 사라진다.** 해제하는 코드는 없다(`grep -n "register_handler\|strategy_manager" src/execution/safety/factory.py src/execution/safety/runtime.py` 0건). **P0-3/P0-4 의 교훈이 그대로 재현된 사례다 — live writer 를 막았더니 그 writer 가 겸하던 emit 까지 사라졌다.**

#### (다) 새 결함 — 같은 (종목·side·전략)의 두 번째 보호 SELL 은 영구히 불가능하다 (차단 25 신규)

- **F7** `SignalGateway._bind` 는 intent 를 `(order.symbol, order.side, order.strategy)` 로 캐시하고 메모리에만 둔다(`src/execution/safety/gateway.py:42`, `:151-156`).
- **F8** `lifecycle.prepare_candidate` 는 같은 intent 의 두 번째 SUBMIT 에 `quantity > _replacement(state, intent_id)` 면 거부한다(`src/execution/safety/lifecycle.py:281-282`), 그리고 `_replacement` 는 `max(0, intent["target_quantity"] - sum(applied_quantity))` 다(`:208`). `target_quantity` 는 **첫 prepare 에서 고정**된다(`:284-285`).
- ⇒ 1차 분할익절 10주(target=10, applied=10)를 낸 종목은 이후 손절 90주를 **같은 intent 로는 영영 낼 수 없다**(`previous attempt unresolved or target exceeded`). 재시도(같은 수량)는 되지만 **새 목표는 안 된다.** 보호 SELL 의 intent 소유권 설계가 P1 의 필수 항목인 이유다.

#### (라) 취소를 한 번이라도 보내면 그 종목과 일자 전환이 영구히 잠긴다 (⑥ 의 근거)

- **F9** 자식(CANCEL) attempt 는 어느 상태로도 terminal 이 되지 못한다: `record_result` 가 kind≠submit 이면 status 와 무관하게 `OrderState.RECONCILING` 을 쓰고(`src/execution/safety/lifecycle.py:496-497`), `reconcile()` 은 첫 검사에서 `attempt["kind"] != "submit"` 을 탈락시킨다(`:515`). `abandon_candidate` 는 **claim 전·미송신** 자식만 끝낸다(`:371-419`, 특히 `:407-409` 의 `command_ref is not None` 거부).
- **F10** 살아남은 자식은 그 종목의 새 SUBMIT 을 `unresolved_child_attempt`(`src/execution/safety/commands.py:382-383`)로, 일자 전환을 `unresolved_child_command`(`src/execution/safety/day_recovery.py:124-126`)로 영구히 막는다.
- **F11** 부모도 끝나지 않는다: 파서는 자식행이 보이거나 읽히지 않은 행이 있으면 `chain=True` 로 두고(`src/execution/safety/evidence.py:242-244`), `supported = (… and not chain and …)`(`:265-272`), 산출 상태는 `FINAL_FILLED if supported else RECONCILING` **둘뿐**이다(`:279`) — 파서에 `FINAL_CANCELLED` 생산 경로가 없다.
- ⇒ **취소 최종성이 "알 수 없다"인 한, CANCEL 을 보내는 순간 부모·자식 둘 다 영구 미종결이고 그 종목과 일자 전환이 잠긴다.** 이것이 P1 설계의 가장 강한 제약이다.

#### (마) pending 하나가 그 종목의 보호를 영구히 침묵시킨다 (⑤ 의 근거)

- **F12** `quote_protection` 은 pending 이 있는 동안 후보의 `pending_since` 를 매 호출 `now` 로 고정한 뒤 계산하고 **결정을 None 으로 덮어쓴다**(`src/execution/safety/protection.py:445-456`). 새 pending 이 생기면 `pending_owners[symbol] = intent_id` 를 세운다(`:457-460`), intent_id 가 None 이면 `ValueError("신규 pending 보호 목표에는 intent_id가 필요합니다")`.
- **F13** 복제 계산기의 `_pending_verifier` 는 항상 non-None 인 보수 표식이라(`src/execution/safety/protection.py:246-252`, `:283`) legacy 의 hard expiry 는 1800초 판이 되는데(`src/strategies/exit_manager.py` 의 `_hard_limit`), F12 의 `pending_since = now` 고정 때문에 **그 만료도 영원히 오지 않는다.**
- **F14** 해제 간선은 실제 SELL 체결(`reduce_protection` 의 `on_fill` 경로, `src/execution/safety/protection.py:410-421`)과 `repair_protection` 둘뿐이다.
- **F15** `pending_owners[symbol]` 의 유효성은 `state["intents"][owner]` 의 실재·side='sell'·`target_quantity == pending_target_qty` 를 요구한다(`src/execution/safety/protection_recovery.py:266-273`, 위반 시 `:393 unknown_pending_owner`). `intents` 항목은 **prepare 의 같은 reduce 에서** 만들어진다(`src/execution/safety/lifecycle.py:272-286`).
- ⇒ **불변식 (I-1): `state['intents'][I]` 가 없다 ⟺ intent `I` 로 POST 가 나간 적이 없다.** prepare 가 intents·attempts 를 한 commit 에 만들고 그 뒤에야 claim/POST 가 있기 때문이다. 이 불변식이 H7 의 해제 판정 전부다.

#### (바) 래치와 고아 접수 행 (차단 23)

- **F16** `_protection_failed` 는 프로세스 수명 bool 이고 해제 코드가 없다: 설정 `src/execution/safety/runtime.py:1310-1311`(quote task 의 취소/commit 뒤 실패)·`:1346`(repair 실패), 읽기 `:465-466`(`market_source_pending`)·`:206-207`(`_quiescence_reason`).
- **F17** durable 접수 행은 **성공한 같은 commit 에서만** 지워진다(`src/execution/safety/runtime.py:1266 del state["protection_quote_admissions"][command_id]`), 종목당 1행만 허용(`:1209-1210`), 남아 있으면 `market_source_pending`(`:463-466`)으로 **전 종목 SUBMIT** 을, `unresolved_quote_admission`(`src/execution/safety/day_recovery.py:119-120`)으로 일자 전환을 막는다.
- **F18** **두 반쪽이 서로를 잠근다.** BLOCKED 로 끝난 `prepare_day_rollover` 도 `_day_closed = True` 를 먼저 세우고(`src/execution/safety/runtime.py:264-265`) 그 래치는 `complete_day_rollover` 의 APPLIED(`:349-350`)에서만 풀린다. `day_admission_closed`(`:148-155`)가 참이면 `quote()` 는 머리에서 `_require_day_admission()`(`:1164`)으로 막히므로, **고아 행을 만든 바로 그 경로로는 고아 행을 풀 수 없다.** ⇒ 재개 연산은 일자 admission 을 요구해서는 안 된다.

#### (사) 게이트·예약·세션 (③④ 의 근거)

- **F19** `unresolved_symbol_attempt` 는 side 를 보지 않고(`src/execution/safety/commands.py:393-394`) 예약 검사(`:422-426 reserved_quantity_insufficient`)보다 **앞**이다 ⇒ 미체결 BUY 가 같은 종목의 보호 SELL 을 막는다(F8①).
- **F20** late-fill 의 벽은 두 줄이다: 전량 청산이 `del portfolio.positions[symbol]` + 그 종목 모든 lot 을 `closed=True` 로 만들고(`src/execution/safety/economics.py:460`, `:464`), 이후 부모 BUY 의 늦은 체결이 `:385 raise ValueError("종결된 진입의 늦은 체결은 대사가 필요합니다")` 로 터진다. 정지 낱말은 **두 개이고 순서가 있다** — `_owner_ready` 의 `unapplied_execution_observation`(`src/execution/safety/commands.py:106-107`, `_evaluate` 의 `:361` 호출이 attempt 순회보다 앞)이 먼저, 그 다음이 `unresolved_execution_evidence`(`:385-387`). 둘 다 **종목·side 무관 전역**이다.
- **F21** 세션 라벨은 6구간이고(`src/execution/safety/requests.py:61-70`) `prepare_submit` 은 `break`/`closed` 를 전면 거부, `pre_close`/`closing` 의 MARKET 을 거부한다(`:243-246`). `pre_market`/`next_market` 은 `ORD_DVSN='05'`+`AFHR_FLPR_YN='Y'`(`:250-254`). **main 의 브로커도 같다** — `main:src/execution/broker/kis_kr.py:485-494`(closed/break 거부, 동시호가 시장가 거부).
- **F22** prepare↔dispatch 사이 세션이 바뀌면 attempt 는 durable 하게 종료된다(`request_session_changed` → `_unsent` → `abandon_candidate` → `FINAL_REJECTED`·`command_status='not_sent'`·예약 해제; `src/execution/safety/commands.py:364`, `:614`, `:632-633`, `src/execution/safety/lifecycle.py:412-417`).
- **F23** exit_exempt 의 load-bearing 가드는 `src/strategies/exit_manager.py:946-948`(`if symbol in self._exit_exempt: return None`) 하나다 — owner 의 복제 계산기가 같은 함수를 쓰므로 **면제 종목에는 결정 자체가 생기지 않는다.** engine 브랜치에는 main 의 on_signal 중앙 가드(PR #83)가 없다(`grep -n "_is_exit_exempt" src/core/engine.py` 0건; 있는 것은 축출 제외용 `_exit_exempt_ref` `:1509`, `:1700`).
- **F24** 취소 POST 는 킬스위치를 의도적으로 건너뛰고 `EV_CANCEL` 로만 원장에 남으며(`src/execution/safety/transport.py:188-198`), SUBMIT 은 킬스위치·`EV_SUBMIT` 을 통과한다(`:186-197`) — P0-1 로 닫힌 계약이 보호 SELL 에도 그대로 적용된다.

#### (아) 입력 계약 (①의 근거)

- **F25** `observe_market` 은 WS 전용이다 — 파서가 `type(event) is MarketDataEvent` 와 `type(event.observation) is MarketObservation`, `source=='kis_websocket'` 을 요구한다(`src/execution/safety/market_source.py:81-95`). REST 는 `quote()` 직접 호출뿐이다.
- **F26** `market_as_of`/`source`/`source_event_id` 는 셋 다 주거나 셋 다 None 이어야 하고(`src/execution/safety/runtime.py:1172-1180`), **셋 다 None 이면 stale 검사 자체를 건너뛴다**(`:451-452 if observed is None: return`). 즉 REST 입력에는 stale 보호가 없다 — legacy 와 같은 수준이다(`main:src/execution/broker/kis_kr.py` 의 `FHKST01010100` 파싱에 체결시각 필드가 없다 — 조사 C L2 인용, 이 설계에서 재확인하지 않았다).
- **F27** 보호 전용 입력(entry_observation 없음)은 그 종목의 진입 증거를 무효화한다(`src/execution/safety/runtime.py:1262-1264 invalidated_at_version`), 그러면 `source_is_current`(`src/execution/safety/market_source.py:200-203`)가 False 가 되어 BUY 가 `current_market_source_required`(`src/execution/safety/commands.py:399`)로 막힌다. **REST 피드는 WS 커버 종목을 제외하므로**(`src/schedulers/kr_scheduler.py:4934-4941 `ws_covered`) 한 종목에 두 출처가 섞이는 것은 WS 접속/절단 전환 순간뿐이다.
- **F28** `observe_market` 경로의 `market_data` 는 자유 dict 가 아니다 — OHLC 계열 키는 원관측과 같아야 하고 `low` 는 **항상 원관측 값으로 덮어쓰인다**(`src/execution/safety/market_source.py:11-37`). 생산자가 실을 것은 `ma5`·`prev_low` 뿐이다.
- **F29** 시세 입력은 전 종목 직렬이고(`_quote_lock`, `src/execution/safety/runtime.py:1270-1271`) 건당 durable 쓰기 2회(admit·reduce)이며, 그 동안 `market_source_pending` 이 참이라 **전 종목 SUBMIT 이 막힌다**(`src/execution/safety/commands.py:362-363` — side 무관, 보호 SELL 포함).

#### (자) main 의 기준선 — 재확인분

- **M1** `stale_order_still_live` 는 True/False/None 3값이고 1차 판정이 **브로커 인메모리 장부**다(`main:src/core/engine.py:2394-2398`), 반대 방향 주문은 생존 근거로 읽지 않는다(`:2398` 주석).
- **M2** 판단 불가의 상한은 횟수가 아니라 시간 180초이고 등록 후 총 270초다(`main:src/core/engine.py:2437-2441` 상수·주석, `:2487-2494` 해제).
- **M3** **전량 청산의 즉시 폴백 근거는 매도가능수량 가정이다** — `main:src/core/engine.py:2322-2326` 주석 원문: "전량 매도(손절·트레일링)는 원 주문이 살아 있으면 KIS 가 주문가능수량 0 으로 거절하므로 과매도가 불가능하다". D1 이 "저장소 어디에도 없는 가정"이라고 기록한 그 문장이다.
- **M4** 보유기간 초과(영업일)가 손절보다 **앞**이다(`main:src/strategies/exit_manager.py:960-968` vs `:1043`). engine 브랜치도 같다(`src/strategies/exit_manager.py:960-968` vs 손절 `:1043` 계열 — 차이는 `date.today()` 대 주입 `self._clock().date()` 뿐).
- **M5** SELL 은 매수1호가 지정가가 기본이고 호가가 없으면 시장가다(`main:src/core/engine.py:2522-2534 _get_sell_price`, `:2356-2372`). engine 브랜치도 같은 줄 번호대에 같은 코드가 있다(`src/core/engine.py:2356-2372`, `:2522-2534`).

---

### 2-2. 설계 요지 — 한 문장

**보호 결정에 소비자를 붙이되(①⑤), 그 결정이 만드는 주문을 "취소할 필요가 없는 주문"으로 만든다(②⑥).**

취소 최종성이 알 수 없다는 판단 아래에서 (라)F9~F11 이 말하는 것은 하나다 — **attach 는 CANCEL 을 보낼 수 없다.** 그런데 main 의 3분류·폴백 장치는 전부 "지정가를 시장가로 바꾸기 위한" 장치다(M2·M3). 그러면 지정가를 쓰지 않으면 그 장치 전체가 불필요해진다. P1 은 그 길을 택한다: **보호 SELL 은 처음부터 시장가**(세션이 허락하는 구간에서). 이 한 결정이 ②(3분류·폴백·상한)·③(자식 가드)·④(재준비 대부분)를 **작성하지 않는 코드**로 만든다.

남는 것은 넷이다: 생산자(H1~H5·H9·H12), 시장가 정책(H6), pending 해제(H7), 래치·고아 행 해제(H8). F8① 은 late-fill 이 먼저라 조건부 단계로 뺀다(H10).

---

### 2-3. 설계

#### H1 — 생산자는 새 주기 task 가 아니라 **MARKET_DATA 핸들러**다

신규 부품 `src/execution/safety/protection_producer.py`, 클래스 `ProtectionProducer(runtime, *, clock)`. 공개 표면은 셋: `async def on_market_data(event)`, `def health() -> dict`, `async def sweep()`(H7·H8 의 회수 — 핸들러 머리에서도 부르고 설치기가 기동 시 한 번 부른다).

근거: 시세는 이미 두 피드가 MARKET_DATA 로 나른다(F6). 새 주기 task 를 세우면 같은 종목을 두 번 조회해 KIS 공용 리미터(10/s)를 두 배로 쓰고, pykrx 일봉 캐시(`ma5`/`prev_low`)도 두 벌이 된다. 핸들러로 붙이면 **새 KIS 호출 0건·새 loop 0건**이고, 주기는 자동으로 main 과 같아진다 — WS 커버 종목은 실시간, 미커버는 REST 20초(`src/schedulers/kr_scheduler.py:4903-4908` docstring, `:5016` 근처의 20초 sleep).

등록 지점은 `factory.install_attached_runtime` 하나다(attach 의 유일한 경로). `engine.register_handler` 는 append 이고(`src/core/engine.py:281-284`) `StrategyManager` 는 생성 시점에 이미 등록돼 있으므로(`:1281`), 설치기는 **`_handlers[MARKET_DATA]` 의 맨 앞에 삽입**한다(`insert(0, …)`) — 같은 틱에서 보호가 전략 평가보다 먼저 돌아야 한다. 새 public API 를 만들지 않고 설치기 안에서 리스트를 직접 만지며, 그 한 줄에 이유를 한국어 주석으로 남긴다.

#### H2 — `update_position_price` 는 attach 에서 **무동작**이다 (차단 24)

`src/core/engine.py:945-947` 의 `raise ApplicationBlocked(...)` 를 `return`(무동작)으로 바꾼다. 그 자리에 왜 무동작인지 한국어 주석을 남긴다.

- 막던 결함: "legacy 가 `current_price`/`highest_price` 를 써서 owner 게시본과 어긋나게 만든다".
- 대체 방어: **무동작이 raise 보다 엄격하다.** raise 는 쓰기를 막지 못하고 호출자를 죽일 뿐이며(F5), 실제 탐지기는 `_owner_ready` 의 `legacy_portfolio_writer_conflict`(`src/execution/safety/commands.py:114-118`)다. 그 탐지기는 그대로 남는다. attach 에서 `position.current_price` 를 쓰는 유일한 주체는 owner 다(`src/execution/safety/runtime.py:1282-1285`).

#### H3 — 입력 규약: 종목당 출처 하나, 시각은 있으면 단조·없으면 없음

| 구분 | 호출 | `market_as_of`/`source`/`source_event_id` | `market_data` |
|---|---|---|---|
| WS 틱(`event.observation` 존재) | `runtime.observe_market(event, intent_id=…, market_data=…)` | 원관측이 채운다 | `{'ma5','prev_low'}` 만 — `low`/`high` 는 원관측 소유(F28) |
| REST 틱(observation 없음) | `runtime.quote(symbol, event.close, market_as_of=None, source=None, source_event_id=None, intent_id=…, market_data=…)` | **셋 다 None**(F26) | `{'ma5','prev_low','high','low'}` |

`ma5`/`prev_low` 는 legacy 의 pykrx 일봉 캐시를 **읽기만** 한다 — `src/schedulers/kr_scheduler.py:302`(`_ma5_cache`)·`:4822-4849`(`_refresh_composite_cache`)·`_prev_day_low` 를 생산자가 소유하지 않고 참조한다(캐시 갱신은 legacy 스케줄러가 계속 한다 — 그 코드는 attach 분기 밖이라 본문 불변). 생산자가 캐시를 새로 만들면 일 1회 pykrx 호출이 두 벌이 된다.

**main 의 WS 비대칭(복합 트레일링이 WS 틱에서 죽는 것)은 복제하지 않는다** — 두 경로 모두 `market_data` 를 싣는다. 동등의 하한은 REST 경로다.

#### H4 — intent 소유권: **보호 에피소드당 intent 하나**, 생산자가 굽는다 (차단 25)

(다)F7·F8 때문에 gateway 의 `(종목, side, 전략)` 캐시를 보호 SELL 에 쓸 수 없다. 규칙:

1. 생산자는 종목별로 "현재 에피소드의 intent_id" 를 메모리에 들고 있는다. 없으면 **틱마다가 아니라 사전 검사가 결정을 예고할 때** `'pp-i-' + uuid4().hex` 로 굽는다.
2. 그 값을 `quote()`/`observe_market()` 의 `intent_id` 로 넘긴다 ⇒ `pending_owners[symbol]` 이 그 값이 된다(F12).
3. 같은 값을 SignalEvent 의 `metadata['protection_intent_id']` 로 실어 보낸다.
4. `SignalGateway._bind`(`src/execution/safety/gateway.py:169-183`)는 **SELL 이고 그 키가 있을 때만** 그 값을 쓰고, `self._intents` 캐시를 건드리지 않는다. BUY 와 그 밖의 SELL 은 종전 경로 그대로다.
5. pending 이 풀리면(H7) 생산자는 그 종목의 intent 기억을 버린다 ⇒ 다음 에피소드는 새 intent ⇒ `_replacement` 상한(F8)에 걸리지 않는다.

**checkpoint 스키마 영향 없음** — `pending_owners` 의 모양(`{symbol: intent_id}`)도 `intents` 의 모양도 그대로다. 새로 저장되는 필드는 0개다.

#### H5 — 결정의 소비는 **반환값**으로, outbox 행은 감사용으로

생산자는 `quote()` 의 3-튜플을 그대로 받아 legacy 와 **같은 모양**의 SELL SignalEvent 를 만든다(`main:src/schedulers/kr_scheduler.py:1178-1195` 대조; engine 브랜치의 같은 코드는 `src/schedulers/kr_scheduler.py:1178-1195`):

```
metadata = {"source": "exit_manager", "quantity": q, "exit_action": action,
            "protection_intent_id": I, "order_type": "market"}   # 마지막 둘만 새 키
reason   = decision[2]                                            # exit_type 태그의 정본
```

`reason` 문자열이 그대로 `classify_exit_type` 을 거쳐 `exit_type` 이 된다(`src/execution/safety/gateway.py:88-96`, `src/utils/exit_types.py`) — 문구 규약을 바꾸지 않는다.

발행은 `engine.emit` 이 아니라 **`await engine._submit_signal(event)` 직접 호출**이다. attach 에서 SIGNAL 은 어차피 그 함수로 직행하므로(`src/core/engine.py:536-539`) 동작은 같고, **동기적**이라 세 가지를 공짜로 얻는다: ⓐ `market_source_pending` 이 내려간 직후 창에서 제출한다(F29), ⓑ prepare 가 `intents[I]` 를 만들기 전에 프로세스가 죽어도 (I-1)이 성립한다, ⓒ 한 틱의 보호가 끝나기 전에 다음 틱이 들어오지 않는다(핸들러 루프가 직렬, `src/core/engine.py:637`).

**outbox 의 `protection_decision` 행은 건드리지 않는다.** 상태 전이를 만들지 않는 이유: 재발행 방지는 이미 owner 가 한다 — 결정 뒤에는 `pending_stage` 가 서고 이후 모든 quote 가 `decision=None` 을 돌려주므로(F12), 소비 표식이 없어도 같은 결정이 두 번 나오지 않는다. 새 상태 0개가 정답이다.

다만 `health()['outbox_pending']`(`src/execution/safety/runtime.py:1492-1493`)은 `status != 'delivered'` 를 세므로 이 행들이 영구 누적으로 잡힌다 — `journal_delivery._pending()`(`src/execution/safety/journal_delivery.py:220-222`)이 이미 제외하는 것과 같게 **`protection_decision` 을 제외하도록 한 줄 맞춘다**(H13-b). 두 계수기가 어긋난 채로 두면 운영자가 미배달 체결을 읽지 못한다.

#### H6 — 보호 SELL 은 시장가다. 그래서 취소·3분류·폴백이 없다

`RiskManager.on_signal` 의 SELL 분기(`src/core/engine.py:2356-2377`)에 attach 전용 분기를 넣는다: `_execution_runtime is not None` 이고 `event.metadata.get("order_type") == "market"` 이면 `_get_sell_price` 를 부르지 않고 `OrderType.MARKET`(price=None)으로 Order 를 만든다. 다른 모든 경로는 바이트 동일.

- `prepare_submit` 은 MARKET 을 `regular`/`pre_market`/`next_market` 에서 받는다(F21). `valuation_price` 는 `order.price` 가 None 이면 `event.price`(그 틱 가격)로 채워지므로(`src/execution/safety/gateway.py:179-180`) 차단 사유 10("가격 없는 SELL 거부")에 걸리지 않는다.
- `pre_close`(08:50~09:00)·`closing`(15:20~15:30)에서는 MARKET 이 build 단계 거부다(F21). 이 두 구간에서만 생산자가 `order_type` 키를 싣지 않아 **legacy 의 지정가 경로(매수1호가, M5)로 강등**한다 — main 의 브로커 정책과 정확히 같다(`main:src/execution/broker/kis_kr.py:490-494`).
- `break`(15:30~15:40)·`closed` 에서는 생산자가 결정을 계산하되 **발행하지 않고** 보류 사유를 health 에 남긴다(`prepare_submit` 이 어차피 `unsupported_submit_session` 으로 끝난다). 그 구간에서는 `quote()` 도 부르지 않는다 — 부르면 pending 없이 결정만 버려진다.

이 한 결정이 다음을 **설계하지 않는다**로 만든다: 3분류(소멸/생존/판단 불가), 판단 불가 180초 예산, 20/60초 재시도, 하트비트 구동 재시도, 시장가 폴백 2회 상한, 동시호가 취소 전 분기, 자식 가드 side 인지, `cancel_all_for_symbol` 의 인메모리 장부 문제, `get_exchange_open_orders` 를 증거 계약 안에 들일지의 질문.

대가는 둘이고 둘 다 이름을 붙인다: **(ㄱ) 스프레드 1틱** — 보호 SELL 이 main 보다 비싸다(매도 수수료·세 0.213% 대비 미미). **(ㄴ) `closing` 에 처음 발화한 보호 SELL** 은 지정가이고 미체결이면 취소할 수 없어 차단 22(부분/미체결 주문의 마감 소멸)로 간다. (ㄴ)는 §2-9 의 사용자 결정 항목이다.

#### H7 — `release_protection_pending` — 나가지 않은 SELL 의 pending 을 푼다 (차단 23 앞쪽)

신규 `runtime` 메서드 하나. `owner.mutate` 한 번, reducer 는 순수 판정이다.

```
해제 조건(둘 다 owner state 만 본다):
  owner = state['protection']['pending_owners'].get(symbol)
  ① owner is not None
  ② owner not in state['intents']                      ← (I-1): POST 가 나간 적 없다
     OR (intent 의 모든 attempt 가 TERMINAL_STATES 이고 sum(applied_quantity) == 0)
해제 동작: 그 종목의 pending_stage/pending_since/pending_target_qty/pending_filled_qty 를
  None/0 으로 되돌리고 pending_owners[symbol] 을 지운다. stage(current_stage) 는 건드리지 않는다.
```

- ②의 첫 갈래가 성립하는 이유는 (I-1)이다 — `prepare_candidate` 가 intents 와 attempts 를 **한 reduce 에서** 만들고(`src/execution/safety/lifecycle.py:272-286`) 그 뒤에야 claim/POST 가 있으므로, intents 에 없으면 그 intent 로 나간 주문이 없다. 시각 판정을 **짓지 않는다**(차단 22 에서 거부한 종류와 같은 계열을 피한다).
- ②의 둘째 갈래는 `abandon_candidate`(미송신 포기)·NOT_SENT·REJECTED 로 끝난 attempt 를 덮는다. **부분 체결(applied>0)은 해제하지 않는다** — 그쪽은 `on_fill` 이 이미 stage 를 승격시킨 정상 경로다.
- 호출자는 생산자의 `sweep()` 하나. 매 틱 머리에서 돈다(전 종목 O(n), owner 쓰기는 해제할 것이 있을 때만).
- **재발행 간격**: 같은 종목의 해제 후 재발행은 생산자 메모리의 `PROTECTION_RETRY_SECONDS = 60.0` 으로 제한한다. 재시도 **횟수 상한은 두지 않는다** — 상한에 닿아 보호를 그만두는 것이 재시도보다 나쁘다. 상한 없는 재시도가 주문 폭주가 되지 않는 이유는 구조적이다: UNKNOWN 으로 남은 attempt 는 비terminal 이라 ②가 거짓이고, 따라서 재발행이 시작되지 않는다.

#### H8 — `resume_protection_admission` — 고아 접수 행과 래치를 푼다 (차단 23 뒤쪽)

신규 `runtime` 메서드. **`_require_day_admission()` 을 부르지 않는다** — F18 의 상호 잠금 때문이다. 이 연산은 새 노출도 새 증거도 만들지 않고, **이미 durable 하게 접수된 요청을 완료하거나 명시 폐기**할 뿐이라 admission 이 닫힌 상태에서 도는 것이 맞다(`reconcile`/replay 와 같은 부류).

```
각 고아 행 (command_id, request) 에 대해:
  A. 저장된 market_as_of 가 현재 floor(_quote_time_floor) 보다 이르면
     → 행만 삭제하고 disposition='stale_admission_discarded' (낡은 가격으로 보호를 계산하지 않는다)
  B. 아니면 runtime.quote() 의 reduce 와 **같은 reducer** 를 그 command_id 로 한 번 돌린다.
     성공하면 결정 3-튜플을 호출자(생산자)에게 돌려주고 생산자가 H5 로 소비한다.
  C. B 가 예외로 끝나면 재시도하지 않는다(PROTECTION_RESUME_MAX = 1):
     행을 삭제하고 그 종목을 protection.degraded 에 넣는다
     (_degraded, src/execution/safety/protection.py, reason='protection_calculation_failed')
     + disposition='protection_admission_abandoned' + CRITICAL 로그.
```

- B 는 **`complete_source` 를 부르지 않는다** — 보호 반쪽만 재개하고 진입 증거는 무효 상태로 둔다. 차단 23 이 경고한 "낡은 진입 증거로 BUY 게이트를 여는 ①M3 위험"을 구조적으로 배제한다.
- C 가 P0-4 심사의 "결정적 reducer 실패는 고치지 못한다"에 대한 답이다: **한 종목을 degraded 로 격리하고 소리친다.** degraded 종목은 `quote_protection` 이 즉시 `(dto, None)` 을 돌려주므로(`src/execution/safety/protection.py:449-450`) 그 종목의 보호가 멈춘다 — 그 사실을 숨기지 않고 health 와 텔레그램 경보로 올린다.
- `_protection_failed` 래치는 **성공한 quote commit 에서 해제**한다(`src/execution/safety/runtime.py:1305-1312` 의 done_callback 에 한 줄). 안전한 이유: 고아 행이 남아 있으면 `market_source_pending`(`:463-466`)이 행 자체로 계속 참이므로, 래치를 풀어도 게이트는 닫힌 채다. 래치는 "쓰기가 실패했다"만 뜻하고, 성공한 쓰기가 그 사실을 반증한다.

#### H9 — EOD 는 생산자의 시각 게이트로 이식한다

갭EOD·테마EOD 는 ExitManager 밖에 있다(`main:src/schedulers/kr_scheduler.py:1114-1172` / engine 브랜치 `src/schedulers/kr_scheduler.py:1095-1151`). 생산자가 같은 판정을 한다: `_now_hm >= "15:10"` 이고 전략이 `gap_and_go` 면 손익 < 0%, `theme_chasing` 이면 손익 < +1% → 전량 SELL. 손익은 **owner 게시본의 position(avg_price)과 그 틱 가격**으로 계산한다(legacy 와 같은 입력).

이것이 시세 틱 구동이라 "시세가 끊기면 EOD 도 죽는다"는 성질이 있는데, **main 도 정확히 같다** — main 의 EOD 도 `_check_exit_signal` 안, 즉 같은 틱 구동이다. 동등.

#### H10 — F8① 은 두 부분으로, 둘째는 조건부다

**H10-a (P1 필수, 게이트를 열지 않는다).** 생산자는 매 틱 `gateway.unresolved_symbols()`(`src/execution/safety/gateway.py:122-125`)를 읽어, 보호가 필요한데 그 종목에 미해결 attempt 가 있으면 **발행하지 않고 `protection_blocked_by_open_entry` 를 health·CRITICAL 로그로 남긴다.** 오늘도 결과는 같지만(어차피 `unresolved_symbol_attempt` 로 막힌다) **조용하던 것이 시끄러워진다** — P0-3/P0-4 의 교훈("막으면 그 writer 가 겸하던 emit 까지 사라지는지 묻는다")을 이 자리에 적용한 것이다. 이 상태로는 여전히 차단 사유이며, 설치 전제로 남는다.

**H10-b (조건부 — S4, 실패하면 P2 로 밀고 차단 사유로 기록).** 순서는 B28 이 옳다: late-fill 수용이 먼저, side 인지가 나중.

1. `src/execution/safety/economics.py:456-464` 의 `if full:` 블록에서 **그 종목에 예약이 남은 비terminal 진입 attempt 가 있으면** `del portfolio.positions[symbol]`·원가/수수료 행 삭제·`lot['closed']=True` 를 **하지 않는다**(포지션은 수량 0 으로 남는다).
2. 그 뒤에야 `src/execution/safety/commands.py:393-394` 의 `unresolved_symbol_attempt` 를 side 인지형으로 연다.

**중단 기준(미리 정한다):** 1 의 RED 가 `encode_portfolio`/`decode_portfolio`/`evaluate_entry_policy`/`publish_protection` 중 어느 하나에서 "수량 0 포지션"을 거부하는 것을 보이면 **S4 를 중단하고 F8① 을 차단 사유로 남긴다.** 돈 경로의 포지션 표현을 바꾸는 것은 P1 의 범위가 아니다.

#### H11 — 스케줄러/배치에만 있던 청산의 처분

| 항목 | 처분 | 근거 |
|---|---|---|
| 갭EOD·테마EOD | **이식한다** (H9) | main 동등의 필수 항목 |
| RSI2 전용 청산(`batch_analyzer`, FDR 일봉 RSI(2)>70) | **범위 밖으로 선언** | `rsi2_reversal` 은 폐지 전략이다(`enabled=false`·allocation 0%, CLAUDE.md 전략표) — 발화 대상이 0건이다 |
| `batch_analyzer` 의 **달력일** 보유기간 강제 청산 | **이식하지 않는다** | owner 경로가 ExitManager 의 **영업일** 기준을 이미 갖는다(M4). 두 기준의 병존은 main 의 결함이지 계약이 아니다. 단 달력일이 먼저 발화하므로 이쪽이 **덜 보호적**이다 — §2-8 에 "미달(경미)"로 올리고 §2-9 의 결정 항목으로 둔다 |
| 코어홀딩 조기경보(-12%·MA200 3일) | **범위 밖** | 코어 배분이 0% 다(CLAUDE.md 배분표: core 0 / gap 15 / sepa 40 / vcp 10) |

#### H12 — 유량·직렬성 상한과 "결정 있는 틱은 스로틀을 우회한다"

- `PROTECTION_QUOTE_INTERVAL = 20.0`(초, 종목별 owner 쓰기 최소 간격 — main 의 REST 주기와 같다).
- 매 틱 생산자는 먼저 **순수 사전 검사**를 한다: `quote_protection(owner.state['protection'], symbol=…, price=…, now=…, market_data=…, intent_id=I)` 를 호출하고 반환 dto 를 **버린다**. 이 함수는 입력을 복제해서 계산하므로(`src/execution/safety/protection.py:441-461`) owner 를 건드리지 않고 I/O 도 없다.
- 결정이 None 이 아니면 **스로틀을 무시하고** 즉시 `quote()`/`observe_market()` 를 부른다. 결정이 None 이면 스로틀이 만료됐을 때만 부른다.
- ⇒ **손절·트레일링·익절의 발화 지연은 main 과 같다(틱 즉시).** 스로틀이 희생하는 것은 `highest_price` 워터마크의 표본 정밀도뿐이다 — 20초 사이의 순간 고점을 놓치면 트레일링이 그만큼 늦게(=덜 공격적으로) 발화한다. §2-8 에 "미달 1건"으로 올린다.
- ceiling 주석(한국어)으로 남길 것: 사전 검사가 틱마다 `_validate(dto)` + `decode_protection` 을 돈다. 보유 8종목·WS 틱 기준의 CPU 가 문제가 되면 종목별 "결정 없음 가격 밴드" 캐시가 다음 단계다. 지금 만들지 않는다.
- `market_source_pending` 이 SUBMIT 을 전역으로 막는 창(F29)은 **틱당 owner 쓰기 2회**뿐이고, H5 의 동기 제출이 그 창이 내려간 직후에 오므로 자기 자신의 발행을 막지 않는다. 다른 종목의 틱이 그 사이에 끼어들 수 없는 이유는 핸들러 루프가 직렬이기 때문이다(`src/core/engine.py:637`).

#### H13 — 관측

- **H13-a** `runtime.health()` 에 `protection` 절을 더한다: `producer_running`, `last_tick_at`, 종목별 `last_quote_at`, `throttled`, `blocked_by_open_entry`(종목 목록), `pending_released`, `resume_dispositions`(카운터), `reemissions`, `degraded`(기존). 교착 관측은 **pending 등록 시각을 되감지 않는다** — H7 은 시각이 아니라 owner state 로 판정하므로 되감을 값 자체가 없다.
- **H13-b** `health()['outbox_pending']`(`src/execution/safety/runtime.py:1492-1493`)에서 `protection_decision` 행을 제외한다(H5 참조).
- **H13-c** 하트비트: 생산자는 자기 이름(`kr_protection_producer`)으로 `record_attempt`/`record_success`/`record_idle`(보유 0건)을 남긴다 — legacy 피드와 같은 규약.
- **보호 생산자 생존을 BUY 게이트로 쓰는 것은 P1 범위 밖이다**(§2-10). `reconciler_live`(`src/execution/safety/commands.py:376`)와 대칭인 `protection_producer_unavailable` 은 분명히 더 안전하지만 **새 전역 BUY 차단**이고, P1 의 과제는 보호 SELL 동등이지 새 매수 게이트가 아니다. health 필드로 먼저 관측하고 P2 에서 판단한다.

---

### 2-4. 게이트를 여는 변경 — 막던 결함의 이름과 대체 방어

P1 이 여는 게이트는 **넷**이다. 각각 막던 결함을 먼저 이름 붙인다.

| # | 여는 것 | 그 게이트가 막던 결함(이름) | 대체 방어 |
|---|---|---|---|
| G1 | `engine.update_position_price` 의 attach raise → 무동작 (H2) | **legacy 가격 writer 가 owner 게시본을 덮어써 `legacy_protection_writer_conflict`/`legacy_portfolio_writer_conflict` 를 만든다** | 무동작은 쓰기를 **실제로** 막는다(raise 는 호출자만 죽였다). 탐지기 `_owner_ready`(`commands.py:114-118`)는 그대로. attach 에서 `current_price` 의 유일한 writer 는 `runtime.py:1282-1285` |
| G2 | `_protection_failed` 를 성공 commit 에서 해제 (H8) | **commit 뒤 실패한 보호 쓰기 위에서 새 노출을 만든다** | 고아 접수 행이 남아 있으면 `market_source_pending` 이 **행 자체로** 계속 참이다(`runtime.py:463-466`). 래치는 그 사실의 중복 표현일 뿐이고, 성공한 쓰기가 "쓰기가 실패했다"를 반증한다 |
| G3 | 고아 `protection_quote_admissions` 행의 재개/폐기 (H8) | **아무도 받지 않는 보호 결정을 커밋해 `pending_stage` 를 영구히 세운다(손절이 조용히 사라진다)** + **낡은 진입 증거로 BUY 게이트를 연다** | ⓐ 이제 소비자가 있다(H5) — 재개가 낸 결정은 같은 tick 안에서 SignalEvent 가 된다. ⓑ 재개는 `complete_source` 를 부르지 않아 진입 증거는 무효 그대로다. ⓒ 시각이 floor 보다 이르면 재개하지 않고 폐기한다. ⓓ 결정적 실패는 1회로 끊고 그 종목만 degraded 로 격리 + CRITICAL |
| G4 | (조건부 S4) `unresolved_symbol_attempt` 의 side 인지 (H10-b) | **전량 청산이 lot 을 닫은 뒤 부모 BUY 의 늦은 체결이 들어와 `unapplied_execution_observation` → `unresolved_execution_evidence` 로 전 종목 영구 정지**(F20 — 정지 낱말은 두 개이고 `unapplied_execution_observation` 이 먼저 닿는다) | 순서를 뒤집는다: **late-fill 수용이 먼저**(예약이 남은 진입이 있으면 lot 을 닫지 않는다), 그 다음 side 인지. late-fill 수용 없이 side 인지만 열면 곧바로 전역 정지를 만든다 |

**여는 것이 아닌 것(명시):** `market_source_pending` 의 SUBMIT 전역 차단, `unresolved_execution_evidence`/`unapplied_execution_observation` 의 전역 범위(차단 19·D4), `trading_ready`(계속 False), `unresolved_child_attempt`, 차단 22 의 만료 계약 — P1 은 이 다섯을 손대지 않는다.

---

### 2-5. 대안과 기각 이유

| 대안 | 기각 이유 |
|---|---|
| **A1. main 의 3분류를 그대로 attach 로 옮긴다**(취소 → 소멸/생존/판단 불가 180초) | CANCEL 을 한 번 보내면 자식은 영구 RECONCILING(F9), 부모는 `chain=True` 로 영구 미종결(F11) — 그 종목과 일자 전환이 잠긴다. 열려면 취소 최종성을 인정해야 하고 그것은 Q7~Q12 판단과 D1·D2 의 금지선이다 |
| **A2. 파서의 `chain` 을 "부모 행이 전량 체결(filled==qty, remaining==0, cancelled==0)일 때"만 완화한다** | 취소 최종성이 아니라 체결 최종성의 확장이라 논리는 성립하지만, **성공한 취소**(cancelled>0)는 여전히 영구 미종결이다. 즉 이 완화로도 A1 은 못 산다. 증거 계약을 건드리는 비용만 남는다 |
| **A3. 세션 종료를 근거로 자식 attempt 를 닫는다** | 만료 판정을 짓는 것이라 P0-4 가 차단 22 에서 거부한 종류와 같은 계열이다 |
| **A4. 자식 명령을 attempt 로 만들지 않고 부모 행의 필드로 기록한다** | 요청 바운드 파이프라인(예약·dispatch·transport·감사)이 전부 attempt 모양이다. 재설계 비용이 P1 을 넘는다 |
| **A5. 보호 SELL 도 지정가로 내고 미체결이면 그냥 둔다**(취소 없음, 에스컬레이션 없음) | 손절이 체결되지 않는다. legacy 보다 명백히 덜 안전하고 차단 22 가 매일 터진다 |
| **A6. 전용 주기 task 로 생산자를 만든다**(`start_reconciler` 대칭) | 같은 종목을 두 번 조회해 KIS 공용 리미터를 두 배로 쓰고 pykrx 캐시가 두 벌이 된다. MARKET_DATA 는 이미 두 피드가 낸다(F6) — 공짜인 입구를 두고 새 입구를 만들 이유가 없다 |
| **A7. 결정을 outbox 행에서 읽어 소비 표식(`status` 전이)을 남긴다** | 재발행 방지는 owner 의 `pending_stage` 가 이미 한다(F12). 새 상태 전이는 순수 추가 비용이고 outbox 검증기·보호 replay 와의 상호작용 표본을 새로 만들어야 한다 |
| **A8. quote 의 `intent_id` 를 gateway 의 `_intents` 에서 가져온다** | `_replacement` 상한(F8) 때문에 그 종목의 두 번째 보호 SELL 이 영구 불가능해진다. intent 는 **에피소드** 단위여야 한다 |
| **A9. H7 의 해제를 pending 경과 시간으로 판정한다** | `quote_protection` 이 매 호출 `pending_since = now` 로 고정하므로(F12) 경과가 영원히 0 이다. owner state(I-1)로 판정해야 한다 |
| **A10. 사전 검사 없이 매 틱 owner 에 쓴다** | 틱당 durable 쓰기 2회 + 그 동안 전 종목 SUBMIT 정지(F29). WS 틱 속도로는 owner 가 상시 pending 이 된다 |
| **A11. 사전 검사 없이 20초 스로틀만 건다** | 손절 접촉이 최대 20초 늦어진다 — main 의 WS 실시간 손절보다 명백히 미달. 사전 검사는 그 미달을 "워터마크 표본 정밀도"로 줄인다 |

---

### 2-6. 인수 — RED 먼저, 변이 kill 로 확인

원칙: **각 항목은 RED 를 먼저 만들고**, 그 뒤 "이 변경이 지키는 불변식을 지우면 그 시험이 죽는가"를 변이로 확인한다.

| ID | RED(먼저 실패해야 한다) | 죽여야 하는 변이 |
|---|---|---|
| **R1** (H2/차단 24) attach 에서 MARKET_DATA 를 한 건 emit 하면 `engine.stats.errors_count` 가 0 이고 전략 핸들러가 끝까지 돈다 | `update_position_price` 의 attach 분기를 `raise` 로 되돌리면 죽는다 |
| **R2** (H1/H5) 보유 종목의 손절가 아래 틱 한 건 → owner 에 SELL attempt 가 생기고 요청 본문의 `ORD_DVSN='01'`·`SLL_TYPE='01'`, `exit_type` 태그가 `stop_loss` | 생산자 등록을 지우면 죽는다 / `_submit_signal` 대신 `emit` 으로 바꾸면 "같은 tick 안"이 깨져 죽는다 |
| **R3** (H3) WS 틱 경로에서 복합 트레일링(MA5·전일저가)이 발화한다 | `market_data` 를 싣지 않으면(= main 의 WS 비대칭을 복제하면) 죽는다 |
| **R4** (H3) REST 틱은 `market_as_of`/`source`/`source_event_id` 가 **셋 다 None** 으로 들어간다 | 셋 중 하나라도 채우면 `ValueError`/`future_market_quote` 로 죽는다 |
| **R5** (H4/차단 25) 1차 분할익절 10주 체결 후 같은 종목·전략의 손절 90주가 **성공한다** | intent 를 `(종목,side,전략)` 캐시에서 가져오도록 되돌리면 `previous attempt unresolved or target exceeded` 로 죽는다 |
| **R6** (H4) `pending_owners[symbol]` 과 prepare 가 만든 `state['intents']` 키가 같고 `target_quantity == pending_target_qty` 다 | intent_id 를 quote 와 gateway 에서 따로 굽게 하면 `protection_recovery._pending_known` 이 `unknown_pending_owner` 로 죽는다 |
| **R7** (H7) 세션 경계 소멸(`request_session_changed` → NOT_SENT)로 끝난 보호 SELL 뒤, **다음 틱에서 같은 결정이 새 intent 로 다시 나간다** | H7 을 지우면 `pending_stage` 가 남아 결정이 None 이 되어 죽는다 — **"pending 하나가 보호를 영구 침묵시킨다"(F12)의 kill 표본** |
| **R8** (H7) prepare 직전에 프로세스가 죽은 시뮬레이션(`pending_owners` 에 있고 `intents` 에 없다) → `sweep()` 이 해제한다. 반대로 attempt 가 있고 `applied_quantity > 0` 이면 **해제하지 않는다** | 해제 조건에서 `sum(applied)==0` 을 지우면 둘째 단언이 죽는다(부분 체결의 stage 승격을 되감는 변이) |
| **R9** (H8/차단 23) 고아 접수 행 1건 + `_protection_failed=True` + `day_admission_closed=True` 인 checkpoint 에서 `resume_protection_admission()` 이 행을 풀고 결정을 돌려준다 | `_require_day_admission()` 을 넣으면 `day_transition_admission_closed` 로 죽는다 — **F18 상호 잠금의 kill 표본** |
| **R10** (H8) 저장된 `market_as_of` 가 floor 보다 이른 고아 행은 재개되지 않고 `stale_admission_discarded` 로 폐기된다 | 무조건 재개로 바꾸면 낡은 가격으로 계산된 결정이 나와 죽는다 |
| **R11** (H8) reducer 가 결정적으로 실패하는 고아 행은 **1회만** 시도되고 그 종목이 `protection.degraded` 에 들어간다 | 재시도 상한을 지우면 무한 루프로 죽는다(시험은 호출 횟수를 센다) |
| **R12** (H8) 재개는 `market_sources[symbol]` 의 `invalidated_at_version` 을 되돌리지 않는다 → 그 종목 BUY 는 여전히 `current_market_source_required` | 재개에 `complete_source` 를 넣으면 죽는다 — **G3 의 ①M3 방어 kill 표본** |
| **R13** (H6) `closing`(15:25) 틱의 보호 SELL 은 LIMIT 으로 나가고, `break`(15:35) 틱에서는 **아무것도 발행되지 않으며** quote 도 호출되지 않는다 | `order_type='market'` 을 세션 무관하게 실으면 `unsupported_submit_session` 으로 죽는다 |
| **R14** (H9) 15:10 이후 `gap_and_go` 손실 포지션 틱 한 건 → 전량 SELL, `theme_chasing` +0.5% → 전량 SELL, +1.5% → 없음 | 시각 경계(15:10)나 손익 경계를 흔들면 죽는다 |
| **R15** (F23/면제) `exit_exempt` 종목의 손절가 아래 틱 → 결정 0·SUBMIT 0. 그리고 quote 뒤·제출 직전에 `engine._exit_exempt_ref` 에 등록되는 경합에서도 발행되지 않는다 | 생산자의 제출 직전 재검사를 지우면 둘째 단언이 죽는다 |
| **R16** (H10-a) 미체결 BUY 가 있는 종목의 손절 틱 → SUBMIT 0 **이고** health 의 `blocked_by_open_entry` 에 그 종목이 있다 | 관측 기록을 지우면 "조용한 미발화"로 돌아가 죽는다 |
| **R17** (H12) 결정이 있는 틱은 스로틀을 무시하고 즉시 owner 에 쓴다 / 결정이 없는 틱은 20초 안에 두 번 쓰지 않는다 | 스로틀 우회를 지우면 첫 단언이 죽는다(손절 지연 변이) |
| **R18** (H13-b) `protection_decision` 행이 3건 있어도 `health()['outbox_pending']` 이 0 이다 | 제외를 지우면 죽는다 |
| **R19** (미설치 경로) attach 가 아닌 경로에서 `_check_exit_signal`·`monitor_positions`·`update_position_price`·`on_signal` SELL 분기의 동작이 **바이트 동일** | 기존 S4-0 특성화 26건이 그대로 GREEN 이어야 한다 |
| **R20** (S4 조건부, H10-b) BUY 100/40 적용 → 보호 SELL 40(전량) → 잔여 60 의 늦은 체결이 **수용된다**(포지션 재구성, receipt APPLIED) | `if full:` 의 진입-예약 조건을 지우면 `종결된 진입의 늦은 체결은 대사가 필요합니다` 로 죽는다 |

**추가 고정(회귀 방지, 기존 계약을 깨지 않았음을 보인다):** 자식 종료 가드 전체(`tests/test_execution_child_command_close.py`), 세션 경계 소멸·재준비(`tests/test_execution_dispatch_reasons.py:547`·`:582`), 부분 체결 예약 산술과 side 무관 잠금(`tests/test_execution_p04_reservation.py`), UNKNOWN 1건의 전역 정지(`tests/test_execution_owner_gate_authority.py:1002`·`:1013`), attach 의 stale pending 무동작(`tests/test_execution_p04_stale_pending.py`) — P1 은 이 다섯을 **하나도 바꾸지 않는다**(H10-b 를 진행하는 경우 `test_execution_p04_reservation.py` 의 side 무관 표본만 결정으로 갱신한다).

**미고정 갈래의 해소:** 조사 B 가 "표본 0"이라고 적은 다섯 중 셋을 P1 이 채운다 — ① 결정이 소비되는 갈래(R2), ③ late-fill 전 시퀀스(R20, 조건부), ⑤ closing 세션의 보호 SELL(R13). ②(ACK 된 CANCEL 의 종결)는 **취소를 보내지 않으므로 도달 불가로 남는다**. ④(`reserved_quantity_insufficient`)는 F-B1-6 이 말한 대로 같은 종목 충돌에서는 도달 불가이며 P1 이 바꾸지 않는다.

---

### 2-7. 단계 분할

전제: 이 호스트는 운영 서버다 — **pytest 동시 ≤2**, 전체 suite 는 단독 직렬(읽기 전용 에이전트도 없이). live 파일을 만진 단계는 지정 파일 GREEN 만으로 통합하지 않는다. 각 단계 뒤 **전체 suite(UTC/KST 각 1회)**.

| 단계 | 범위 | 파일(writer 1인) | 병렬 | 게이트 |
|---|---|---|---|---|
| **S1** 부품 | H7·H8·H13-b: `release_protection_pending`, `resume_protection_admission`, `_protection_failed` 해제, outbox 계수 정정 | `src/execution/safety/runtime.py` | W1 | R7~R12·R18 RED→GREEN. 제품 호출자 0건 |
| **S1'** 부품(동시) | H4 의 gateway 쪽: `_bind` 가 SELL 의 `protection_intent_id` 를 받는다 | `src/execution/safety/gateway.py` | W2 (S1 과 동시 — 파일 분리) | R5·R6 의 gateway 절반 |
| **S2** 부품 | H1·H3·H5·H9·H10-a·H12: 생산자 신설 | `src/execution/safety/protection_producer.py`(신규) | W1 (S1 뒤) | R2~R4·R14~R17 RED→GREEN. 아직 제품 등록 없음 |
| **S3** live #1 | H2(차단 24) + H6(시장가 분기) | `src/core/engine.py` **하나** | 단독 | R1·R13·R19. **전체 suite 필수** |
| **S4** live #2 | 설치기 배선: 생산자 등록(`_handlers[MARKET_DATA]` 맨 앞), `sweep()` 기동 호출 | `src/execution/safety/factory.py` **하나** | 단독 | 설치기 거부 목록에 항목 추가·차단 21 닫힘 표기. **전체 suite 필수** |
| **S5** 문서 | `kr_scheduler.py`·`batch_analyzer.py` 의 차단 21 주석을 "닫힘(P1)"으로 갱신 — **본문 0줄** | 주석만 | 단독 | `git diff --stat` 으로 본문 불변 확인 |
| **S6** 조건부 | H10-b: late-fill 수용 → side 인지 | `economics.py` 먼저, 그 다음 `commands.py` (순서 의존, 같이 하지 않는다) | 단독 | R20. **중단 기준(H10-b) 에 걸리면 여기서 멈추고 차단 사유로 기록** |

병렬 상한: 코디네이터 1 + 작업자 최대 3(전 공급자 합). 실제 배치는 **S1‖S1' 두 명 + 독립 재현 1명**(2+1)이 최대이고, S3 이후는 단독이다. 모델·effort: 부품 구현은 `gpt-5.6-terra`/high, 돈 경로(S3·S6)는 `gpt-6-astra` 또는 확인된 `claude-opus-5`/high, 최종 독립 리뷰는 구현자와 분리해 다른 공급자·xhigh.

**S4-0 특성화 26건의 처분(D2 의 2건):** 이 설계는 legacy 본문을 바꾸지 않으므로 P1 에서는 26건 모두 GREEN 이어야 한다. main 을 engine 에 들이는 날의 처분(재작성 vs legacy 동기화)은 **P1 범위 밖**이며 §2-9 의 결정 항목으로 남긴다.

---

### 2-8. main 동등의 수용 기준 — 조사 A 체크리스트 1:1 대조

| # | 조사 A 의 항목 | 판정 | 근거 / 미달이면 차단 사유 |
|---|---|---|---|
| 1 | 주기 생산자가 owner quote 를 부르고 SELL 을 emit | **동등** | H1·H5. main 의 20초 REST·WS 두 구동기를 **같은 입구(MARKET_DATA)** 로 받는다 |
| 2 | `market_data` 부착, main 의 WS 비대칭은 복제하지 않는다 | **더 안전** | H3·R3. main 은 WS 틱에서 복합 트레일링이 죽는다(`main:scripts/run_trader.py:1932` 가 인자 2개) |
| 3 | 8종 발화 + 판정 순서(보유기간 계열이 손절보다 앞) | **동등** | owner 가 **같은 `ExitManager.update_price`** 를 복제 계산기로 돌린다(`protection.py:254-276`) — 순서가 정의상 같다(M4) |
| 4 | 갭EOD·테마EOD | **동등** | H9·R14. main 과 같은 시각·손익 경계, 같은 틱 구동 |
| 5 | RSI2·batch 달력일 보유기간의 명시 결정 | **미달(경미) 1건** | H11. RSI2 는 폐지 전략이라 범위 밖(동등). **달력일 보유기간은 이식하지 않아 청산이 main 보다 늦다** → §2-9 결정 항목 |
| 6 | 3분류의 1차 판정이 인메모리 장부가 아니다 | **더 안전(해당 없음)** | H6. **취소를 보내지 않으므로 판정할 대상이 없다.** main 이 인메모리 장부로 오판하는 경로(`main:engine.py:2394-2398` + `kis_kr.py` 의 재시작 후 빈 캐시)가 attach 에는 존재하지 않는다 |
| 7 | 판단 불가 180초 상한·생존은 무기한 유지 | **더 안전(해당 없음)** | 같음. 시장가는 판단 불가 상태를 만들지 않는다 |
| 8 | 20/60초 재시도·SIGNAL 없이 구동·주기 처리 상한 | **더 안전(해당 없음)** | 같음. 재시도는 **틱 구동**이고(main 의 하트비트 등가물보다 촘촘하다) H7 의 60초 재발행 간격이 상한이다 |
| 9 | 전량/분할 분기가 등록 시점 의도로 결정된다 | **동등** | `decision[0]`(`sell_all`/`sell_partial`)이 결정 그 자체다 — 잔고 스냅샷 비교를 하지 않는다. 수량은 같은 스냅샷에서 나온 `decision[1]` |
| 10 | 즉시 폴백이 매도가능수량 팩트에 기대지 않는다 | **더 안전** | H6. main 은 M3 의 미확인 가정 위에서 살아 있을 수 있는 지정가 위에 시장가를 얹는다. attach 는 지정가를 만들지 않으므로 중복 노출이 구조적으로 불가능하다. D1("매도가능수량 팩트를 만들지 않는다")을 지킨다 |
| 11 | 폴백 2회 상한과 상한 뒤 처리 | **더 안전(해당 없음)** | main 의 D5-① 결함(상한 뒤 취소 미송신·원 지정가 방치)을 복제하지 않는다 |
| 12 | 동시호가 취소 전 분기·15:30~15:40 거부·closing 지정가 강등 | **동등, 잔여 1건** | H6·R13·F21. 세션 정책은 main 의 브로커와 **같은 표**다. 잔여: **closing 에 처음 발화한 보호 SELL 이 미체결이면 차단 22 로 간다**(main 은 취소·재주문이 가능하다) → §2-9 결정 항목 |
| 13 | 세션 경계 재준비의 소유자 | **동등** | H7·R7. main 은 "전량 취소 + 장부 비우기 + 다음 날 생산자가 재생성"이고, attach 는 "소멸을 owner 가 durable 하게 기록(F22) + 생산자가 재준비". 소유자는 양쪽 다 **생산자**다. 횟수 상한 대신 60초 간격 — 상한에 닿아 보호를 포기하지 않는다 |
| 14 | 미체결 BUY 뒤 같은 종목 보호 SELL 이 막히지 않는다 | **미달 1건(S6 성공 시 동등)** | H10-a 가 관측 가능하게만 만든다. **S6 을 중단하면 차단 사유로 남는다** — 이름: "미체결 BUY 가 같은 종목의 보호 SELL 을 막는다(`unresolved_symbol_attempt`, side 무관)" |
| 15 | late-fill 대비("취소 0건의 흔한 원인은 방금 체결") | **더 안전(해당 없음)** | 취소를 보내지 않으므로 "취소 0건"이라는 모호한 신호 자체가 없다. 체결은 P0-2 의 reconciler 가 증거로 배운다 |
| 16 | 생존 판정이 반대 방향 주문을 근거로 읽지 않는다 | **더 안전(해당 없음)** | 생존 판정 자체가 없다 |
| 17 | exit_exempt 가드가 모든 SELL 경로에 | **동등** | F23·R15. main 의 9곳은 **경로가 9개**이기 때문이고, attach 의 SELL 발행 경로는 생산자 하나다 — 결정 생성부(`exit_manager.py:946-948`)와 제출 직전 재검사 2곳으로 같은 보장을 얻는다. main 의 "면제 종목의 기 발행 SELL 은 취소만" 분기는 attach 에 기 발행 지정가가 없어 해당 없음 |
| 18 | await 중 면제 등록 경합을 두 곳에서 재검사 | **동등** | R15 둘째 단언(quote 시점 + 제출 직전 `engine._exit_exempt_ref`) |
| 19 | 래치·durable 행 해제가 결정 소비자가 있는 상태에서만 | **동등(차단 23 닫힘)** | H8·G3·R9~R12. 소비자가 생기는 **같은 설계 안에서만** 해제한다 |
| 20 | 킬스위치·감사 원장 통과 | **동등(회귀 없음)** | F24. P0-1 이 닫은 계약을 보호 SELL 이 그대로 탄다(SUBMIT 이므로 킬스위치 검사 대상) |
| 21 | 유지 중인 SELL 이 헬스/하트비트에서 보인다 | **동등** | H13. pending 등록 시각을 되감는 경로가 없다(H7 은 시각을 쓰지 않는다) |
| 22 | 미설치(legacy) 경로 바이트 동일 + D2 2건 결정 기록 | **동등** | R19·S5. legacy 본문 0줄. D2 2건의 처분은 main 을 들이는 날의 별도 결정으로 §2-9 에 올린다 |
| **보너스** | main 에 없는 것 | **더 안전** | 차단 24(시세 틱마다 전략 평가 사망)·차단 25(두 번째 보호 SELL 영구 불가)를 P1 이 같이 닫는다 |
| **보너스** | main 에 없는 미달 | **미달 1건** | H12: 20초 스로틀이 `highest_price` 워터마크의 틱 간 극값을 표본에서 놓친다 → 트레일링이 main 보다 늦게(덜 공격적으로) 발화할 수 있다. **손절 발화 지연은 없다**(결정 있는 틱은 스로틀 우회) |

**요약: 동등 12 · 더 안전 7 · 미달 4**(5 달력일 보유기간 / 12 closing 미체결 / 14 F8① / 워터마크 표본). 미달 넷 중 둘(5·12)은 사용자 결정이고, 14 는 S6 의 성패이며, 워터마크는 명시 수용 후보다.

---

### 2-9. 사용자에게 물어야 할 결정 — 코드로 정할 수 없는 것만

1. **보호 SELL 의 시장가 전환(H6)을 승인하는가.** 이것이 P1 전체를 성립시키는 결정이다. 대가는 스프레드 1틱(왕복 수수료·세 0.227% 대비 미미)이고, 얻는 것은 취소·3분류·폴백 전체의 부재와 "취소 최종성 미상" 문제의 완전한 회피다. 거부하면 A1 로 돌아가고 **attach 는 취소 최종성의 공식 답(Q7~Q12)이 올 때까지 설치할 수 없다.**
2. **`closing`(15:20~15:30)에 처음 발화하는 보호 SELL 의 처분.** (가) 지정가로 내보내고 미체결 시 차단 22 를 감수한다 / (나) 그 10분 동안은 보호를 발행하지 않는다(무보호 창을 명시 수용) / (다) EOD 마감 시각을 15:10 에서 앞당겨 그 창에 도달할 확률을 줄인다. **설계의 보수적 기본값은 (가)** 다 — 팔지 못하는 것보다 팔고 잠기는 쪽이 돈에 가깝다.
3. **`batch_analyzer` 의 달력일 보유기간 강제 청산(M-E5)을 attach 에 이식하는가.** 이식하지 않으면 청산이 main 보다 늦고(덜 보호적), 이식하면 영업일/달력일 두 기준이 owner 안에 공존한다. **설계의 기본값은 "이식하지 않는다"**(기준 단일화). 어느 쪽이든 "게이트를 여는/닫는 변경"이라 결함 이름을 먼저 붙였다: 이식 안 함 = "보유기간 청산이 main 보다 최대 (달력일−영업일)만큼 늦다".
4. **H12 의 워터마크 표본 손실을 명시 수용하는가.** 수용하지 않으면 스로틀을 줄여야 하고, 그러면 owner durable 쓰기 빈도와 `market_source_pending` 정지 창이 그만큼 늘어난다(F29). **실측 없이는 어느 쪽도 정할 수 없다** — S2 의 인수에 "보유 8종목 기준 틱당 owner 쓰기 지연"을 측정 항목으로 넣고, 그 수치를 보고 결정하기를 권한다.
5. **S6(F8①)을 P1 에 포함하는가, P2 로 미는가.** 포함하면 돈 경로(`economics.py` 의 포지션·lot 표현)를 건드린다. 미루면 차단 사유가 하나 남는다. **설계의 기본값은 "조건부 포함 + 중단 기준"**(H10-b).
6. **S4-0 특성화 26건 중 D2 의 2건 처분** — main 을 engine 에 들이는 날 (a) main 기준으로 재작성 / (b) legacy 를 main 과 동기화하고 시험을 둔다. (b)는 "미설치 경로 바이트 동일" 제약과 충돌한다. **P1 범위 밖이지만 P1 이 끝나면 곧 닿는다.**
7. **실계좌 스모크(P0-3 §5 의 D10 기록기)** 는 여전히 사용자 확인 대기다. P1 은 취소 응답·생존 조회의 실제 필드를 **하나도 쓰지 않으므로** 스모크 없이 설계·구현·인수가 가능하다 — 다만 설치 전에는 여전히 필요하다.

---

### 2-10. P1 이 닫지 않는 것 (명시)

- **차단 22**(부분/미체결 주문의 마감 소멸) — 만료 계약은 만들지 않는다. 시장가 보호 SELL 은 이 위험을 크게 줄이지만 **하한가 무매수**에서는 시장가도 미체결로 남는다.
- **미체결 BUY 의 정리 부재** — main 의 10분 BUY 타임아웃 취소는 attach 에 없고(F4) 취소 벽(F9~F11) 때문에 P1 이 열지 않는다. 미체결 BUY 가 마감까지 남으면 일자 전환이 BLOCKED 다(차단 22 와 같은 계열). **P1 의 경계이고 남는 차단 사유다.**
- **차단 12**(UNKNOWN SUBMIT 1건의 전역 정지, 보호 SELL·CANCEL 포함), **차단 19**(체결마다 생기는 전 종목 정지 창), **D4 의 게이트 축소** — P2.
- **차단 20**(`entry_policy_context` 재게시자 0건) — 10C.
- **`protection_producer_unavailable` BUY 게이트** — H13-c 의 관측만 P1, 게이트는 P2.
- `trading_ready` 는 계속 False. MODIFY 는 계속 미지원. KR 체결통보(`H0STCNI0`)는 붙이지 않는다(D9).


## 3. 적대적 심사 → coordinator 처분 = 확정 (설계 초안 §2 를 이 표가 덮어쓴다)

| # | 지적(① 돈/상태 · ② 실현) | 처분 |
|---|---|---|
| ①M1·②MF-3 (P0/P1) | H12 사전 검사 ↔ H4 intent 굽기가 순환 — `quote_protection` 은 신규 pending 에 `intent_id` 필수라 새 결정이 나는 틱마다 ValueError, 손절이 영원히 미발화 | **수용.** 종목에 에피소드 intent 가 없으면 **틱 진입 시** `'pp-i-'+uuid4().hex` 를 굽고 사전 검사·커밋 호출에 **같은 값**을 쓴다; 결정이 None 이면 그 값을 버린다(owner 에 쓴 것이 없어 무해). 인수: "결정 없는 틱에서 구운 intent 는 재사용되지 않는다" + R5·R6 은 커밋 호출의 id 가 정본 |
| ①M2·②MF-6 (P0/P2) | EOD(H9)가 `exit_exempt` 를 우회; `engine._exit_exempt_ref` 는 없고 `RiskManager._exit_exempt_ref` 는 축출 제외용이며 engine 브랜치에 쓰는 코드 0건 | **수용.** 생산자는 EOD 포함 **모든 발행 직전**에 `runtime.exit_manager.is_exit_exempt(symbol)`(게시본 — `publish_protection` 이 같은 set 객체를 유지) 를 검사. R15 를 게시본 기준으로 다시 쓰고 "면제 종목은 EOD 로도 팔리지 않는다" RED 추가 |
| ①M3 (P0) | EOD 는 intent 소유권 밖 → 1차 익절 뒤 EOD 전량이 `previous attempt unresolved or target exceeded` 로 영구 거부(차단 25 잔존) | **수용.** H4 를 "생산자가 내는 **모든** SELL" 로 확장 — EOD 도 새 에피소드 intent + `metadata['protection_intent_id']`. R5 에 "EOD 전량 청산이 1차 익절 뒤에도 성공" 추가 |
| ①M4·②MF-2 (P0) | `pre_market`/`next_market` 의 "시장가"는 `ORD_DVSN='05'`(시간외 종가)·`ORD_UNPR='0'` — 시간외단일가로 손절을 판정해 종가 주문이 나가고, NXT 불가 종목 검사(main 브로커)도 attach builder 에 없다 | **수용.** 보호 SELL 시장가 발행 세션은 **`regular` 만**. `closing`(15:20~15:30)은 지정가 강등(§4-2), **`pre_market`·`next_market` 은 발행하지 않고 health 에 보류 사유**(`break`/`closed` 와 같은 처리). §2-8 #12 를 "미달"로 정정하고 **시간외 보호 SELL 부재를 차단 사유로 등록**(여는 것: NXT 종목 검사 이식 + "시간외 종가 주문은 시장가가 아니다"의 가격 계약 — 별도 단계). R13 에 `next_market`(15:45) 표본: `'05'`·`ORD_UNPR='0'` SELL 이 나가지 않는다 |
| ①M5 (P1) | H10-a 가 매 틱 `gateway.unresolved_symbols()` → `_owner_ready` 를 타서 다른 종목 체결 적용 창·admission 닫힌 구간에 보호 계산 자체가 예외로 죽는다 | **수용.** 관측 판정은 owner state 직접 순회(`attempts` 중 `kind=='submit'`·비terminal 의 symbol) 순수 함수. **불변식: 보호 계산·발행 경로는 `_owner_ready` 를 부르지 않는다.** R16 에 "다른 종목 체결이 inbox 미적용인 동안에도 보호 결정이 계산된다" RED |
| ①M6 (P1) | H8 case A(stale 폐기)가 REST 고아 행(`market_as_of` None)에 no-op → 재기동 뒤 몇 시간 전 가격의 손절이 시장가로 나간다 | **수용.** case A 는 접수 시각 `observed_at` 기준(`now − observed_at > PROTECTION_RESUME_STALE_SECONDS`)이 1차, `market_as_of` 가 있으면 floor 검사 병행. R10 을 "market_as_of None 인 REST 고아 행도 오래되면 폐기"로 |
| ①M7 (P1) | degraded 종목은 결정이 영원히 None 이라 20초마다 `quote()` 가 돌고 `capture_quote` 가 degraded 종목에만 증거를 append → durable 무한 증식 | **수용.** 생산자는 `protection.degraded` 종목에 `quote()`/`observe_market()` 를 부르지 않는다(사전 검사에서 판별). 인수: "20초×N틱 뒤 degraded 종목의 보호 증거 event 수 불변". 운영 문서에 "degraded 종목은 repair 전까지 보호가 없다" |
| ①M8 (P1) | 결정 수량 > live `pos.quantity` 면 engine SELL 분기가 조용히 전량으로 바꿔 `pending_target_qty`≠`target_quantity` → `unknown_pending_owner` 로 그 종목 repair 영구 불가·분할익절이 전량 매도로 | **수용.** 발행 직전 `decision[1] <= owner 게시본 position.quantity` 확인, 어긋나면 **발행하지 않고 경보**. H6 attach 분기는 `metadata['quantity']` 를 그대로 쓰고 불일치는 거부로. R6 를 "불일치를 만들어도 주문이 나가지 않는다" RED 로 |
| ②MF-1 (P0) | 생산자가 `ma5`/`prev_low` 에 닿을 경로가 없다(설치기 시그니처에 bot 없음·두 피드 이벤트에 지표 키 없음) → R3·§2-8 #2 달성 불가 | **수용 — (a) 채택.** `install_attached_runtime(…, indicator_source)` 필수 kwarg(`Callable[[str], dict]` — `{'ma5','prev_low'}` 를 돌려주는 조회 클로저). **설치기 제품 호출자가 0건이라 지금은 live writer 가 늘지 않는다** — 호출자 계약(kr_scheduler 의 `_ma5_cache`/`_prev_day_low` 를 읽는 클로저를 넘긴다)은 docstring. R3 은 "생산자가 `indicator_source` 로 받은 market_data 로 복합 트레일링 발화", 변이 "주입 경로를 끊으면 죽는다" |
| ②MF-4 (P1) | H8-B "같은 reducer" ↔ "complete_source 안 부름" 모순; R12 는 기존 source 행이 없는 종목에서 거짓(안전은 유지) | **수용.** 재개는 **재개 전용 reducer**(entry_observation 제거, `market_sources` 행이 있으면 invalidate·없으면 무동작). R12 를 (i) 행 있음 → `invalidated_at_version`·BUY `current_market_source_required` (ii) 행 없음 → 새 행이 생기지 않는다(변이 `complete_source` 삽입) 둘로 |
| ②MF-5 (P1) | "pending_owners 에 있고 intents 에 없음"이 정상 중간 상태인데 그것이 H7 해제 조건이자 `protection_recovery` 의 `unknown_pending_owner` 조건 — reconciler 주기 task 와 교차 | **수용.** (a) `sweep()` 은 틱 머리에서만 돌고 생산자 자신의 quote 커밋~prepare 커밋 사이에는 구조적으로 돌지 않는다(같은 코루틴) — 코드 주석으로 (b) reconciler 는 pending 을 해제하지 않는다(해제 호출자는 sweep 뿐) (c) 그 창에 `repair_protection` 이 들어오면 `unknown_pending_owner` 로 실패하는 것은 **그대로**(제품 호출자 0·P2 관측). 인수: 교차 창 표본 — quote 커밋 직후·prepare 이전에 sweep/reconciler 가 끼어들어도 pending 미해제 |
| ②MF-7 (P2) | EOD 의 선행성 누락(legacy 는 EOD 가 ExitManager 판정보다 앞이고 발화하면 return) | **수용.** EOD 참이면 그 틱에서 `quote()` 를 부르지 않고 EOD SELL 만. R14 에 동시 참 표본(SELL 1건·reason 갭EOD). 인용 `:1091-1150` |
| ②MF-8 (P2) | 보호 결정 생산자가 둘 — `intraday_preemptive`(장중 급락 선제 stale) 행은 P1 뒤에도 소비자 0, H13-b 의 일괄 제외가 그것까지 숨긴다 | **(b) 채택.** P1 은 소비하지 않고 **새 차단 사유로 등록**("장중 급락 선제 stale 결정의 소비자 0건"). H13-b 는 `effect_source` 로 갈라 `protection_decisions_pending`(quote)·`preemptive_decisions_pending`(intraday) **따로 센다**(①notes 도 같은 권고) |
| ①notes | 기동 sweep 은 `_require_day_admission` 을 타면 안 된다(아침 admission 닫힘) / 재개 결정은 admission 닫힌 창에서 소비 불가 | **수용.** H7·H8 모두 admission 게이트를 부르지 않는다(reconcile/replay 부류). H8 문구: "재개는 고아 행을 없애는 것이 목적, 결정 소비는 admission 이 열린 뒤 다음 틱" |
| ②notes | H1 핸들러 삽입은 구간 2(live engine 변경)·실패 시 철회 미정 / H8 disposition 저장 위치 미정 / `_degraded` 호출 모양 / R16·R17 항진 위험 / 인용 드리프트 | **수용.** 핸들러 삽입은 구간 2 의 **마지막**(attach 뒤·`start_reconciler` 뒤) — 실패 시 철회하지 않는다(그 뒤 실패는 이미 live 가 저장본 값이라 호출자는 legacy 로 못 돌아간다는 기존 계약과 같다). disposition 은 **health·로그만**(durable 새 행 0 — R10/R11 은 in-process 단언). `_degraded(dto, symbol, quantity)` 모양. R16 은 "health 기록 + owner state 순회가 일어났다", R17 은 결정 없는 틱 억제와 **한 쌍**. 인용 드리프트는 구현 시 재확인(§2 의 줄 번호는 ±1~4) |

**처분 뒤 §2-8 판정 정정:** #2 "더 안전"(MF-1 (a) 로 회복) · #12 **미달**(시간외 발행 없음 — 차단 사유) · #17·#18 **동등**(게시본 면제 검사로 회복) · #14 **미달**(F8 ① 은 P2 — §4-5). 요약 **동등 12 · 더 안전 6 · 미달 4**(달력일 보유기간·시간외·F8 ①·워터마크).

**새 차단 사유 후보(인계 표 — Do 착수 시 등록):** 24 attach 에서 MARKET_DATA 틱마다 `update_position_price` 의 raise 로 전략 평가가 죽는다(H2 가 닫는다) · 25 두 번째 보호 SELL 이 gateway `(종목,side,전략)` intent 캐시의 `_replacement` 상한으로 영구 불가(H4 가 닫는다) · 26 시간외(`pre_market`/`next_market`) 보호 SELL 부재 · 27 `intraday_preemptive` 결정 소비자 0건 · (P2 로 미룬) F8 ①.

## 4. 사용자 결정 요청 — 코드로 정할 수 없는 것 (Do 착수 전)

| # | 결정 | coordinator 추천 | 대가 |
|---|---|---|---|
| **4-1** | **보호 SELL 을 시장가로 전환(H6, `regular` 세션 한정)** — P1 전체를 성립시키는 결정. 거부하면 대안 A1(main 의 취소·3분류·폴백 이식)인데 그것은 취소 최종성 인정이 전제라 Q7~Q12 판단·D1·D2 와 충돌하고 attach 설치가 무기한 막힌다 | **승인 추천** | 스프레드 1틱(왕복 수수료·세 0.227% 대비 미미). **유동성 낮은 종목에서는 시장가가 호가를 더 걷을 수 있다** — 현재 보유(펩트론 087010 은 exit_exempt 라 대상 아님)·배분 전략(sepa/gap/vcp)의 유동성 필터가 이를 얼마나 막는지는 실측 항목 |
| 4-2 | `closing`(15:20~15:30)에 처음 발화하는 보호 SELL: (가) 지정가로 내고 미체결이면 차단 22 감수 / (나) 그 10분은 발행하지 않음 / (다) EOD 시각을 앞당김 | **(가)** — 팔지 못하는 것보다 팔고 잠기는 쪽 | 미체결 지정가는 취소할 수 없어 다음 날 일자 전환이 BLOCKED |
| 4-3 | `batch_analyzer` 의 달력일 보유기간 강제 청산 이식 여부 | **이식 안 함**(기준 단일화 — owner 는 ExitManager 의 영업일 기준) | 보유기간 청산이 main 보다 최대 (달력일−영업일) 늦다 |
| 4-4 | H12 20초 스로틀의 `highest_price` 워터마크 표본 손실 수용 | **S2 실측 뒤 결정**(틱당 owner 쓰기 지연을 인수에 측정) | 트레일링이 main 보다 덜 공격적일 수 있음(손절 지연은 없음) |
| 4-5 | S6(F8 ① — late-fill 수용 + side 인지 잠금)을 P1 에 포함 | **P2 로 미룸(coordinator 결정)** — 돈 경로의 포지션·lot 표현 변경은 별도 단계, P1 은 보호 SELL 동등에 집중 | 차단 사유 "미체결 BUY 가 같은 종목 보호 SELL 을 막는다" 잔존(H10-a 로 관측 가능해짐) |
| 4-6 | S4-0 특성화 26건 중 D2 2건 처분 | P1 범위 밖 — main 병합 날 결정 | — |
| 4-7 | 실계좌 스모크(P0-3 §5 D10 기록기) 착수 | 사용자 확인 대기(P1 설계·구현·인수는 스모크 없이 가능, 설치 전 필요) | — |

## 5. 단계(확정 — §4-1 승인 뒤 착수)

| 단계 | 범위(처분 반영) | writer | live | 인수 |
|---|---|---|---|---|
| **S1** | H7 `release_protection_pending` · H8' 재개 전용 reducer + `observed_at` stale 폐기 + degraded 1회 격리 + 래치 해제(성공 commit) · H13-b 갈라 세기 | `src/execution/safety/runtime.py` | 없음 | R7~R12(수정판)·R18·교차 창 표본 |
| **S1'** | H4 gateway `_bind` 가 SELL 의 `protection_intent_id` 를 받는다(캐시 무접촉) | `src/execution/safety/gateway.py` | 없음 | R5·R6 gateway 절반 (S1 과 동시 — 파일 분리) |
| **S2** | 생산자 신설(H1·H3(indicator_source)·H5·H9(선행·면제)·H10-a(순수)·H12(틱 진입 intent)·M7·M8) | 신규 `src/execution/safety/protection_producer.py` | 없음 | R2~R4·R14~R17(수정판)·M7 증거 불변·M8 불일치 미발행. 제품 등록 없음 |
| **S3** | H2(차단 24: raise → 무동작) + H6'(attach 분기: `order_type=='market'` 이고 `regular` 면 MARKET, `metadata['quantity']` 그대로) | `src/core/engine.py` 하나 | 1 | R1·R13(`next_market` 표본 포함)·R19. **전체 suite** |
| **S4** | 설치기: `indicator_source` kwarg·생산자 등록(구간 2 마지막)·기동 `sweep()`·거부 목록 | `src/execution/safety/factory.py` 하나 | 1 | 설치기 인수·차단 21·24·25 닫힘 표기. **전체 suite** |
| **S5** | `kr_scheduler.py`·`batch_analyzer.py` 의 차단 21 주석 갱신 — 본문 0줄 | 주석만 | — | 본문 불변 확인(diff stat) |

병렬: S1‖S1'(2) + 독립 재현 1 = 2+1. S3 이후 단독. 모델·effort: S1/S1'/S2 opus/high(구현) + opus/xhigh(재현), S3·S4 는 돈 경로라 opus/high + Codex(astra/xhigh) 교차 리뷰 필수. 각 단계 뒤 전체 suite UTC/KST(시계 창 회피는 `bba260c` 뒤 불필요). **금지:** trading_ready 강제 True · MODIFY · CANCEL 송신 · 만료 계약 · `market_source_pending`/`unresolved_execution_evidence` 범위 축소 · legacy 본문 수정 · 새 checkpoint 스키마 행 · `_owner_ready` 를 보호 계산 경로에서 호출.

## 6. Do·See (착수 시 채운다)

- **Codex 사전 검토(2026-09-22, 제품 기준 `dff0e25`):** [인계 대조·계약 충돌 처분 제안](../../reviews/p1-handoff-preflight-2026-09-22.md). **Do 미착수.** S1 H8 A/C의 source 무효화 누락, 폐기 뒤 래치와 일자 admission의 상호 잠금, H7 교차 창 인수 모순, 계산 오류와 저장/게시 실패의 구분, 중복 성공과 새 commit 구분을 정적으로 확인했다. 기존 §3의 확정 인터페이스를 조용히 변경하지 않으며, 이 다섯 경계의 처분 전 S1 구현은 보류한다. S1′은 독립 준비 가능하나 착수 계획 확인 전이다. 사용자 4-1~4-4·4-7 및 외부 리뷰 예산 미답, 운영 변경 없음. 아래 기존 착수 게이트와 공급자 치환 처분은 유지한다.

- **인계(2026-09-22 밤):** P1 Do·See 부터는 Codex 세션이 수행한다 — 인계 프롬프트 `docs/operations/codex-handoff-p1-2026-09-22.md`(적대적 검토 2관점 반영). coordinator(Claude) 처분 2건을 여기 기록한다: ① **착수 게이트 완화** — §5 제목의 "§4-1 승인 뒤 착수"를 "4-1 미답이면 S2 부터 착수 금지, S1·S1' 은 RED·GREEN·통합까지 허용"으로(둘은 제품 호출자 0건의 부품이고 차단 23·25 를 위해 4-1 과 무관하게 필요) ② **모델 배정의 공급자 치환** — 구현·재현은 `gpt-6-astra`(high/xhigh), 교차 공급자 리뷰는 `claude-opus-5`/xhigh(`scripts/dev/claude_review.py`); actual model 미확인 리뷰는 교차 리뷰로 세지 않고 live 단계(S3·S4) 통합을 보류한다.
