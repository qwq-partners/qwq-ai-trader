# B2/B3 — 실제 주문 요청에 묶인 qualification·최종 사이징 (실행 계획)

> 2026-09-20 · 기준 `feature/engine-safety-design-20260917` `214223e`(제품 트리 = C4 `ab044c4`) ·
> 상위: `2026-09-18-engine-writer-migration.md` 10B, 인계 `docs/operations/claude-migration-handoff-2026-09-20.md` §1.
> 상태 표기는 **계획**이다. 이 문서의 어떤 항목도 구현·검증 완료를 뜻하지 않는다.
> **단계별 진행·증거·체크리스트·잔여는 `docs/reviews/b2b3-stage-ledger-2026-09-20.md`(단계 원장)에 있다.** 리뷰하거나 이어받을 때는 원장부터 읽는다.
> 운영 경계 불변: main 병합·배포·재시작·주문·설정·Toss grant 변경 없음, `trading_ready=False` 유지, MODIFY 미지원 유지.

## 0. 조사로 확정한 사실 (읽기 전용 3관점 + coordinator 소스 대조)

정적 추적 결과이며 런타임 관측이 아니다. 줄 번호는 `214223e` 기준.

1. **경제·슬롯·섹터·재진입·sync 판정은 이미 owner 에 있다** (`risk_policy.evaluate_entry_policy` + `policy_snapshot` + `commands._evaluate`).
   B2 의 신규 범위는 CV 규칙 1~12 · LLM 2차 검증 · 시간 규칙 · 전략/팩터 예산 · 쿨다운의 fact 화와 **수량 재유도**뿐이다. owner 에 게이트를 이중 정의하지 않는다.
2. `commands._evaluate`(209-279)가 prepare 와 final(`_bound`→`_evaluate`, 마지막 await 이후 동기)의 **단일 공통 관문**이다.
   현재 재검사하지 않는 것: qualification 출처, 수량 정당성(B1 kernel 은 safety 패키지에서 import 0건), `binding['source_versions']` 대조.
3. CV 의 4개 입력(trade_memory·expert_orchestrator·sector_council·panel_outlook)에 version publisher 가 없다 → 10B-2 "publisher 먼저"가 그대로 적용된다.
4. qualification 이 입력 SignalEvent 를 8곳에서 in-place 변형한다(`event.score` 덮어쓰기, `position_multiplier` 를 event/signal 두 dict 에 각각 ×0.5 등). kernel 은 `signal.signal.metadata` 만 읽는다(2757).
5. `EffectiveRiskPolicy` 에는 `base_position_pct`·`min_position_value`·전략 배분이 **없다**. `risk_per_trade_pct`·수수료율은 wrapper(config/FeeCalculator)와 owner(policy) 두 곳에서 온다.
6. runtime attach 후 `engine.py:527-533` 이 SIGNAL/ORDER/FILL 을 `errors_count += 1` 후 **조용히 버린다**. 대체 경로(gateway)는 없다. safety 패키지 밖에 `RequestBoundCommands`/`PolicyContext` 생성자가 0건이다.
7. owner 를 우회하는 KR 송신점: `engine.py:1947`(on_signal 내부 90초 MARKET SELL 폴백 — 거래시간 게이트보다 먼저 실행), `kr_scheduler.py:6754·6818`(KOFR), `7597`(수동 매수), CLI 2개.
   취소 0건을 최종성으로 단정해 예약을 푸는 곳: `engine.py:1987-1991`, `kr_scheduler.py:978-1004`.
8. `trading_ready` 는 property 로 항상 False 이고 `_owner_ready(dispatch=True)` 가 claim 전에 강제한다. 기존 시험은 `monkeypatch` 로만 dispatch 를 구동하며 주석이 "명시 합성 startup 허가"라고 밝힌다.

## 1. 계약 — immutable decision facts

**원칙: "현재 경제"는 매번 owner snapshot 에서, "판단 시점에 굳는 것"은 불변 facts 에서, mutable event/Order/metadata 에서는 아무것도 읽지 않는다.**

| 구분 | 값 | final 에서의 취급 |
|---|---|---|
| 현재 경제 (snapshot) | equity, cash, 다른 attempt 의 예약, 보유/pending 의 전략 노출, effective_daily_pnl, `policy.*`(sizing_mode·risk_per_trade_pct·risk_max_position_pct·max_position_pct·core_allocation_pct·daily_max_loss_pct·buy_commission_rate) | 매 평가 시 다시 읽는다 |
| 판단 시점 (facts) | symbol·side·strategy·origin·intent_id, sector, signal_score, CV original/adjusted score·적용 규칙 ID, LLM verdict, 강도·position·calendar·volatility·conviction 배율, 손절 결정(pct/source/crash_capped)·atr_pct, 설정 전용 값(base_pct·strategy_allocation_pct·min_position_value), `config_version`, `decided_at`·`expires_at`(aware KST), 소비한 출처 `(name, version, as_of, digest)` | digest 가 binding 과 같아야 하고, `config_version`·소비 출처 version 이 **현재값과 같아야** 하며, `now < expires_at` |
| 재유도 | 위 둘로 `position_sizing_kernel.compose_sizing` 재실행 | `result.reason is None and request.quantity <= result.quantity` |

- 위치: 새 모듈 `src/execution/safety/decisions.py`(frozen dataclass, `to_dict/from_dict`, digest 는 기존 `policy_generations.canonical` + `protection_recovery.digest` 재사용).
- 게시: `RequestBoundCommands.publish_qualification_source(name, *, as_of, digest, expected_version)` → `state['qualification_sources'][name] = {version, as_of, digest}`,
  `publish_decision_facts(facts, *, expected_version)` → `state['entry_decision_facts'][intent_id]`. 모양은 기존 `publish_policy_context`/`observe_entry_quote` 와 동일(당일·과거성·expected_version).
- 소비: **AUTOMATIC BUY SUBMIT 만** facts 필수. USER·SAFE_ASSET·SELL·CANCEL 은 현행 유지(청산 판단은 protection owner 소관).
  `_prepare` 가 `binding['decision_facts_digest']` 를 굳히고, sector 는 facts 값이 정본(호출자 kwarg 는 같거나 None).
- stale 범위: 소비한 출처의 version 변화·config 변화·만료만 stale. **무관한 fill/ACK/다른 종목 관측은 stale 이 아니다**(owner.version 전역 증가와 구분).
- 기존 allow bool 재사용 금지, on_signal 복제 금지, kernel 에 새 phase 추가 금지(B1 한정 승인 동결), CV/LLM 임계값·감점 산식 불변.

## 2. 단계 (순차, 각 단계 RED→GREEN→독립 리뷰→coordinator 통합)

| 단계 | 범위 | 제품 파일(단일 작성자) | 시험 파일 |
|---|---|---|---|
| **S1 (B2a)** | facts DTO·게시 2종·prepare 결합·final kernel 재검사/ stale 판정. engine.py 무접촉 | `safety/decisions.py`(신규), `safety/commands.py` | `tests/test_execution_decision_facts.py` |
| **S2 (B2b)** | 실제 출처 publisher: CV 가 frozen facts 를 **추가 반환**(기존 반환·감점 불변), LLM verdict→`size_multiplier` fact, 시간 규칙→`expires_at`, 4개 입력의 version 게시 | `src/core/cross_validator.py`, `src/core/engine.py`(qualification 어댑터 구간만) | `tests/test_execution_qualification_publishers.py` |
| **S3 (B3a)** | runtime attach 시 SIGNAL→기존 후보 판단→gateway prepare→ORDER(command ID 만)→prepared dispatch. 정상 MARKET BUY·LIMIT/MARKET SELL | `safety/gateway.py`(신규), `src/core/engine.py`(dispatch 분기·on_signal 종착부) | `tests/test_execution_signal_gateway.py` |
| **S4 (B3b)** | on_signal 내부 직접 SELL 폴백·취소0건 예약 해제·eviction SELL 을 runtime 모드에서 owner 경로로. UNKNOWN 예약 유지, 취소 ACK≠최종성 | `src/core/engine.py` | 위 파일에 추가 |
| **S5 (See)** | 독립 실큐 인수(구현자 아님) + 최종 broad 리뷰 + UTC/KST 전체 직렬 | 없음(제품 수정 권한 없음) | `tests/test_execution_signal_gateway_acceptance.py` |

- S2·S3·S4 는 같은 `engine.py` 를 만지므로 **순차**다. 10C(KOFR·수동 매수·CLI·scheduler 978-1004)는 범위 밖으로 남긴다.
- 공용 하네스 `tests/test_execution_runtime.py` 는 **동결**(import 만). 새 시험 파일은 각자 `synthetic_home` autouse 를 선언한다.
- **정정(S1 착수 전 확인):** 기존 시험 8건이 공용 `fixture(origin='automatic')` 로 facts 없이 자동 BUY 를 prepare 한다(`test_execution_command_owner.py` 5, `test_execution_command_flow.py` 1, `test_execution_policy_generations.py` 1, `test_execution_policy_generation_acceptance.py` 1). S1 이후 이들은 facts 게시가 필요하므로, **S1 worker 단독**으로 `test_execution_command_owner.py` 의 `fixture()` 에 `facts(req)` helper 를 추가하고 위 8건에 게시 호출만 넣는 최소 수정을 허용한다. 기존 단언의 삭제·완화·기대값 변경은 금지이며 coordinator 가 통합 전 diff 로 확인한다. S2 이후 단계에서는 다시 동결이다.
- legacy(no-runtime) 경로와 US 경로의 동작·기준선(`test_t11_money_path_baseline` 등)은 불변. `Order`/`OrderEvent` 를 frozen 으로 바꾸지 않는다.

## 3. 인수 조건 (RED 는 행동 실패여야 한다 — import/bootstrap 실패는 RED 증거가 아니다)

S1:
1. AUTOMATIC BUY prepare 가 facts 없이 **거부**된다(현재는 통과 → 행동 RED). USER/SAFE_ASSET/SELL/CANCEL 은 facts 없이 현행대로 통과(대조).
2. facts 의 symbol/side/strategy/origin/intent 가 request 와 다르면 거부. 게시된 facts 는 같은 intent 로 다른 내용 재게시 불가(불변).
3. final: prepare 뒤 각 network await 경계(대표 2개) 중 (a) 현금 감소 (b) 다른 attempt 예약 출현 (c) 일손실 축소 구간 진입 (d) 소비 출처 version 변화 (e) config version 변화 (f) 만료 → **NOT_SENT + POST0 + 예약 무변**.
4. 대조: 무관 종목 fill 관측·무관 취소 ACK·소비하지 않은 출처의 version 변화는 stale 이 아니고 POST1.
5. parity: 같은 입력에서 final kernel 수량 == legacy wrapper 수량(`test_execution_sizing_characterization` 의 nominal/risk/core/hybrid 표본, 139/140 경계, MARKET 1.3 affordability).
6. `ready=False`(합성 허가 없음) 대조 시험을 같은 파일에 쌍으로 둔다 — dispatch 는 NOT_SENT, POST0.
7. 시계는 전부 주입(`clock` list). 벽시계 의존 0 — 배포 verify 는 장 마감 후에 돈다.

**S3 는 `2026-09-21-s3-signal-gateway.md`(세부 계획)에서 확정했다** — 하위 단계 S3-1~S3-6b, 그리고 §2 표의 "ORDER(command ID 만)" 은 그 계획의 결정 ③에서 "attach 모드에서는 ORDER 이벤트를 큐에 싣지 않고 gateway 가 같은 command_scope 에서 prepare·dispatch 한다"로 해석됐다(S5 최종 리뷰의 확인 항목). 조사 C 의 RED 후보 24건 원문은 문서로 남아 있지 않아(docs/ 전체 grep) S3 Plan 에서 12항 인수 조건과 인계 Do 1~4 로부터 다시 도출했다.
S4~S5 는 인계 Do 1~4항을 기준으로 S3 통합 뒤 확정한다. 실큐 시험은 "조용한 폐기"를 통과로 오인하지 않도록 `stats.errors_count` 와 gateway 수신 사실을 명시 단언한다.

### S1 독립 리뷰 처분 (2026-09-20, 대상 `03cc2d6..33b7643`)

구현: worker 1명(요청 claude-opus-5/high, 실제 모델 metadata 미노출) — RED `8a571b8` → GREEN `33b7643`, 허용 7파일만 변경.
coordinator 재현: RED 커밋 22 failed/3 passed(행동 RED 1건 `DID NOT RAISE`, 나머지 모듈 부재), GREEN UTC·KST 각 364 passed·격리 0. 기존 시험 4파일 삭제 0줄(Codex 도 `assert` AST 225개 동일 확인).
리뷰: ① Codex 요청 gpt-6-astra/xhigh(읽기 전용 sandbox, 8분 53초) ② Claude 요청 opus/xhigh 2렌즈(돈 경로 parity / 상태·시험 정직성) + 발견 11건 전부 반증 시도(opus/high). **세 리뷰 모두 CHANGES_REQUIRED, P0 0건.** 재검증으로 2건 기각(성장은 기존 잔여 항목, cash·reservation 의 실제 차단 주체는 신규 검사).

| # | 등급 | 발견 | 처분 |
|---|---|---|---|
| R1 | P1 | `recompose_quantity` 에 hybrid 분기가 없다. 운영 설정은 `hybrid.enabled: false` 라 현재 미발동이나, 켜면 legacy 40주 vs final 140~175주(3.5~4.4배)로 **fail-open** (세 리뷰 공통, 수치 재현) | facts 에 설정 전용 값 `hybrid_enabled: bool`(필수) 추가, True 면 `unsupported_hybrid_sizing` 으로 **명시 거부**. 인수 조건 5 의 hybrid 는 parity 표본이 아니라 거부 시험으로 바꾼다 |
| R2 | P1 | `facts.sources=()` 이면 stale 검사가 전무한 채 자동 BUY 가 송신된다 | 자동 BUY facts 는 소비 출처 **1개 이상 필수**(DTO 에서 거부). 실제로 소비가 없는 판단은 S2 에서 ruleset 출처로 표현한다 |
| R3 | P2 | 소비 출처 대조가 version·digest 뿐이다 — 게시본과 다른(미래) `as_of` 도 통과, 전일 게시본으로 당일 매수 정당화 가능 | `_consumed_sources` 가 게시본 `as_of` 동일성·당일(KST)·`as_of <= decided_at` 을 함께 본다 |
| R4 | P2 | final 시험이 `status in (NOT_SENT, UNKNOWN)` 뒤에 예약 0 을 단언 → `UNKNOWN+예약 해제` 회귀를 가린다(실제 status 는 전부 NOT_SENT) | `is NOT_SENT` + 저장 상태 단언으로 좁히고, UNKNOWN 은 별도 시험에서 예약 유지(507,500원·50주)를 단언 |
| R5 | P2 | kernel 재검사의 예약 차감·전략 잔여·overlay 항을 제거한 변이 3종에서 364건 전부 통과. cash·reservation 사례는 신규 검사를 빼도 기존 `position_value_limit`/`cash_insufficient` 가 막는다 | 구속 표본 추가(pending 예약, 전략 예산 소진, overlay≠1.0, 일손실)와 "기존 gate 는 통과·kernel 수량만 부족" 구간의 final 사례(예: 다른 예약 130주 → kernel 44주). **수정 후 변이 (a)(c)(d) 각각 ≥1건 실패가 인수 조건** |
| R6 | P2 | `_bound` 의 `decision_facts_changed` 대조를 지워도 전건 통과 | binding digest 를 위조한 dispatch → NOT_SENT·POST0 시험 추가 |
| R7 | P2 | 인수 조건 4 의 대조가 quote·출처·정책 게시뿐 — 계약이 요구한 **무관 fill 관측·무관 취소 ACK** 가 없다. hashkey 경계도 출처 변화만 본다 | 실제 무관 fill·취소 ACK 대조와 hashkey×경제 변화 1건 추가 |

**계약 문언 정정:** 조건 3 의 "예약 무변"은 "guard 평가 자체는 예약을 바꾸지 않는다"는 뜻이다. 확정 `NOT_SENT` 는 기존 lifecycle 대로 예약을 해제하고(`lifecycle.py` FINAL_REJECTED), `UNKNOWN` 만 `BLOCKED_UNKNOWN` 으로 예약을 유지한다. 두 경우를 한 단언에 섞지 않는다.

**S1 잔여(수정하지 않고 명시):** ① facts 의 설정 전용 값·배율은 게시자 신뢰 범위다(owner 교차검증 없음, 노출은 policy 상한으로 묶임) — S2 publisher 시험에서 설정 출처에 고정한다. ② `entry_decision_facts`·`qualification_sources` 정리 writer 없음(소비는 당일·만료로 fail-closed, 위험은 checkpoint 성장) — `day_recovery.reset_daily` 를 건드리는 별도 작업. 출처 counter 는 재시작·일자 전환에서 단조성을 유지해야 하므로 일별 초기화 금지. ③ wrapper `get_available_cash()` 실산식(최소 현금·레짐 reserve)과 owner `available_cash(regime=True)` 의 등가는 parity 표본이 양쪽을 stub/0 으로 두어 미대조. ④ 과거 checkpoint 의 자동 BUY 는 `decision_facts_required`/`decision_facts_changed` 로 송신 차단(호환 우회 없음).

### S3 사전 확인 (coordinator 소스 대조, `03cc2d6`)

- **세션 표가 둘이다.** legacy `KRSession.get_session`(`src/utils/session.py:125-161`): 08:00~08:50 PRE / 09:00~15:20 REGULAR / 15:40~20:00 NEXT, 그 사이(08:50~09:00, 15:20~15:40)는 전부 CLOSED, 공휴일 반영, **naive `datetime.now()`**. owner `requests._session_at`(64-72): 같은 틈을 `pre_close`/`closing`(MARKET 만 거부)·`break`(전면 거부)로 나누고 휴장은 판단하지 않는다.
  → gateway 는 기존 후보 판단(on_signal 의 거래시간 게이트)을 **먼저** 통과시킨 뒤 owner 로 넘긴다. 순서를 뒤집으면 기존에 막히던 틈 구간에 LIMIT 주문이 새로 열린다. 시간 규칙 fact 의 `decided_at`/`expires_at` 은 aware KST(`runtime._now()`)로 만들고, 실큐 시험은 legacy 벽시계 경로까지 시계를 주입한다.
- **조용한 폐기 지점**: `engine.py:527-533`(`_process_event` 안, handler 호출 전). S3 의 분기는 여기 한 곳이다.
- **eviction**: 호출 `engine.py:2289`(BUY 거부 경로 안), 정의 1586, SELL 신호의 권한 표식은 `metadata['source']='replacement'`(1669) 문자열뿐 → S4 에서 `EntryAuthority` 발급 context 로 대체한다.
- **하위 에이전트 worktree**: 수동 `git worktree add` + worker `EnterWorktree` 는 Bash 격리 고정점 불일치로 실패했다(S1 1차, 커밋 0). Workflow `isolation:'worktree'` + worktree 안에서 `git switch -c work/<이름> <base SHA>` 방식으로 재시도 중이다.

## 4. 역할·모델·한도 (정책 `ai-routing-v1-2026-09-20`)

| 역할 | 요청 모델/effort | 근거 | 한도 |
|---|---|---|---|
| coordinator | 이 세션 | Plan·RED 증거 확인·통합·문서. worker 결과의 모델 검증 주장 없음 | — |
| S1~S4 구현 | claude-opus-5 / high | 돈 경로·상태 무결성(critical implementation) | 단계당 60분, 새 시험 ≤25건, 허용 파일만 |
| 단계별 독립 리뷰 | Codex(gpt-6-astra) / xhigh, `scripts/dev/codex_review.sh` read-only | 구현자와 다른 provider | 1회 + 같은 요청 재시도 1회 |
| S5 인수 | claude-opus-5 / high, **구현 worker 와 다른 실행** | 독립 실큐 인수 | 60분 |
| 최종 broad 리뷰 | Codex Astra / xhigh (불가 시 Opus/xhigh 로 대체하고 "교차 provider 아님" 기록) | critical final review | 1회 |

- active worker ≤3(전 provider 합산), fan-out 금지, 동일 base SHA 의 별도 worktree, 파일당 단일 작성자, coordinator 만 통합.
- worker 금지: 운영 `.env`·토큰·`~/.cache/ai_trader*`·네트워크·systemctl·git push·main/engine 브랜치 직접 수정·허용 목록 밖 파일.
- 실제 모델 메타데이터가 노출되지 않으면 "미검증"으로 기록한다. fallback 결과를 요청 모델 결과로 세지 않는다.

## 5. 보고 규칙

- 구현 완료 ≠ 정상 흐름 이행 완료 ≠ 운영 승격. 합성 `trading_ready` 위의 GREEN 은 "실제 송신 경로 검증"이 아니다.
- 현금 고갈(펩트론 99.6%) 상태라 정상 BUY 의 실경로 표본이 없다 — 정상 흐름 인수는 전부 fake HTTP + 합성 시나리오임을 매 보고에 명시한다.
- 각 단계 종료 시 exact SHA, RED/GREEN 명령·exit code, 리뷰 판정, 미검증 항목을 `docs/reviews/engine-execution-followup-2026-09-18.md` 와 `CHANGELOG.md` 에 남긴다.
