# S2 (B2b) — 실제 qualification publisher 세부 계획

> 2026-09-20 · 기준 engine `e5d0d93`(S1 통합 후) · 상위 `2026-09-20-b2b3-request-bound-qualification.md` · 진행·증거는 `docs/reviews/b2b3-stage-ledger-2026-09-20.md`.
> **상태: 계획.** 어떤 항목도 구현·검증 완료를 뜻하지 않는다. 운영 경계 불변(main 병합·배포·재시작·주문·설정·Toss grant 없음, `trading_ready=False`, MODIFY 미지원).
> 산출 과정: 읽기 전용 조사 2건(CV 출처·갱신 지점 / facts 필드 출처·시간 규칙) → 독립 설계 2안(최소 침습 / stale 실효성) → 심사·종합 1건. 전부 요청 claude-opus-5(high·xhigh), 실제 모델 metadata 미노출. **pytest 0건 — 모든 주장은 정적 소스 대조**이며 심사자가 사실 주장 16건을 파일을 열어 확인했다(15 맞음·1 부분).

## 0. S2 의 목표와 정직한 성과 문장

S2 는 "실제 CV/LLM/시간 규칙/사이징 입력이 불변 `EntryDecisionFacts` 를 **실제로 만든다**"까지다. SIGNAL→gateway 배선(게시된 facts 를 prepare 가 소비)은 S3 다.
S2 가 끝나도 **실효 있는 stale 축은 regime 1개**다. panel_outlook·trade_memory 는 결정 시점에만 게시되고 판단→final 사이 재게시자가 없어 version·digest 대조가 헛돈다(**replay 구속 전용**: 전일·다른 결정의 게시본 재사용과 as_of 당일성만 잡는다). config 축은 제품 `PolicyContext` publisher 가 0건이라 S2 에서는 자기 일관성뿐이고 S3 에서 실효가 생긴다. 보고는 항상 "실효 stale 축 1개(regime) + replay 구속 2개 + config 축은 S3" 로 쓴다.

## 1. 조사로 확정한 사실 (줄 번호 `e5d0d93`)

1. CV 가 받는 `market_regime` 은 runtime 모드에서 owner 의 `effective_regime(state, now)`(`safety/regime_owner.py:175-186`, I/O·await 없는 순수 함수)와 같은 값이다(`engine.py:1830-1841` → `market_regime.py:311-319`). → **final 동기 구간에서 재유도 가능.**
2. runtime 설치 후 `kr_scheduler.py:4610-4620` 이 legacy 2분 regime writer 를 건너뛴다 → 거기에 publisher 를 달면 죽은 코드다.
3. 규칙11(expert)은 `config/default.yml:634 shadow_mode: true` 라 점수·차단 기여 0(`cross_validator.py:546-551` 로그만), 규칙12(sector_council)도 로그만(584-589). 규칙4(섹터)·5(exited_today)의 입력은 owner snapshot 이 이미 소유(`PositionPolicyFact`·`ReentryPolicySnapshot`). trade_wiki 는 `validate()` 가 아니라 LLM 프롬프트 입력.
4. panel_outlook(규칙10)은 주 1회 파일 + CV 자체 6시간 캐시(`cross_validator.py:130-165`), 보너스는 `days_old` 의존이라 파일이 그대로여도 날짜가 바뀌면 값이 달라진다. trade_memory(규칙9)의 `_layer3` 는 20:25 야간 잡에서만 재생성(장중 불변).
5. **tests/ 전체에 `CrossStrategyValidator` 실인스턴스 0건** — "감점 산식 불변"을 고정하는 시험이 없다. `_MIN_PASS_SCORE=50`·`TOTAL_PENALTY_CAP=15`·`_HARD_BLOCK_TAGS` substring 매칭(598-600)이 전부 판정에 쓰인다.
6. `cross_validator.py:258` 이 함수 안에서 `from datetime import datetime as _dt` 로 재임포트 → 모듈 속성 monkeypatch(`_freeze_clock` 관례)로는 09:30/10:30/12:30 경계가 얼지 않는다.
7. CV 인스턴스는 실거래(`engine.py:2053`)와 팀심의 shadow(`kr_scheduler.py:6132-6155`)가 공유하고 그 사이에 `llm_second_check` await(`engine.py:2098`)가 있다 → interleaving 실재. US 는 별도 인스턴스.
8. kernel 이 읽는 `position_multiplier` 는 `signal.signal.metadata` 하나(2755-2757), ATR skip 은 읽은 뒤 1.0 으로 덮는다(2758-2761). LLM soft-reject 는 두 dict 에 각각 ×0.5(2119-2127).
9. overlay 3종은 예외 시 `apply_overlay` 를 부르지 않아 수량상 1.0 과 같고(2765-2792) 구분 수단이 없다. DTO 는 배율 None·0 을 거부한다. `test_execution_sizing_characterization.py:117` 등이 overlay 호출 **순서·인자·횟수**를 못 박아 provider 재호출을 금지한다.
10. `_consumed_sources`(`commands.py:212-228`)는 게시본 `as_of` 의 당일 KST 를 강제 → 주 1회 panel 의 `created_at` 을 `as_of` 로 쓰면 월요일 이후 전부 거부. `publish_qualification_source` 는 호출마다 version+1(180).
11. `publish_policy_context` 제품 호출자 0건(`src/`·`scripts/` 0) — config_version 의 대조 상대가 S2 에는 없다.

## 2. coordinator 결정 (심사자 선택지 ①~⑧ + 구조 변경)

| # | 결정 | 근거 |
|---|---|---|
| ① | S2 허용 제품 파일에 `src/execution/safety/qualification.py`(신규)·`src/execution/safety/commands.py`(S2-3, ~25줄) **추가 승인** (상위 계획서 §2 표의 S2 범위 확장) | commands.py 없이는 regime final 재유도(유일한 실효 stale 축)가 불가. 순수 builder 를 engine.py 에 두면 단위 검증 불가 |
| ② | `hybrid_enabled` 의 policy 대조(S1 잔여 7)는 **S3** | 제품 PolicyContext 행 0건이라 지금 넣으면 자기 자신과 비교. 필수 필드 추가는 기존 시험 동결(상위 계획서 §2)을 깬다. Codex 재리뷰 조건은 **부분 충족**으로 기록, 완전 충족은 S3 인수 조건 |
| ③ | `sources>=1` 은 **regime 을 항상 인용**해 충족(별도 ruleset 더미 없음) | 모든 자동 BUY 가 regime 을 읽고 규칙1·2·3·3-2·3-3 이 거기에 달렸다. ruleset digest 를 따로 두면 config_version 과 축이 겹친다. **상위 계획서 R2 문언("ruleset 출처")에서 이탈 — 여기 기록** |
| ④ | regime_owner 에 writer 훅을 만들지 않는다 — **final 재유도** | 순수 함수라 모든 변경 경로(regime_policy 커밋·intraday_crash_level·일자 게이트)를 한 지점에서 잡는다 |
| ⑤ | 시계 교차검증 `decision_clock_disagreement` 는 **fail-closed** | CV 는 naive `datetime.now()`, owner 는 aware KST. 단 "UTC CI 에서 상시 발화하지 않는다"가 인수 조건 |
| ⑥ | SEPA 14:30 상한을 `expires_at` 에 **포함** | 넣지 않으면 14:29 판단이 14:31 에 송신되는 창이 열린다 |
| ⑦ | engine.py 는 단계별 **허용 구간**을 명시, coordinator 가 통합 전 diff 로 확인 | S2-2(`_calculate_position_size` 내부)·S2-5(어댑터 구간)·S3(on_signal 종착부·`_process_event`)가 겹치지 않게 |
| ⑧ | panel·trade_memory 의 stale 무실효를 **보고에 그대로 쓴다** | 적지 않으면 stale 4축이 전부 작동하는 것처럼 읽힌다 |
| 구조 | 시험 파일을 **하위 단계별로 분리**(종합안은 4단계가 한 파일 공유 → 파일당 단일 writer 위반). 실행은 wave A(S2-1 ∥ S2-2) → wave B(S2-3 ∥ S2-4) → wave C(S2-5) | 호스트 제약으로 pytest 에이전트 동시 ≤2. 같은 wave 의 두 단계는 제품·시험 파일이 서로 다르다 |

## 3. S2 가 새로 고정하는 계약 (S3 gateway 가 의존)

1. 소비 출처는 **판정을 바꾼 것만** 인용한다. shadow 규칙11·12, 보너스 0 인 규칙10, memory_adj 0 인 규칙9 는 인용하지 않는다. 규칙4·5 입력과 trade_wiki 는 출처가 아니다.
2. **regime 은 항상 인용**한다. digest = `canonical({'effective_regime': CV 가 실제로 받은 문자열})`.
3. `as_of` 는 **관측(판독) 시각**이다. 원본 생성시각(panel `created_at` 등)은 digest 안에 넣는다.
4. **regime 은 final 에서 재유도해 대조**한다: `_decision_facts` 가 `effective_regime(state, runtime._now())` 의 digest 를 소비 출처 'regime' 행과 비교(사유 `stale_regime_decision`). `'regime_policy'` 가 state 에 없으면 'regime' 을 소비한 facts 를 **거부**(fail-closed).
5. 출처 게시는 **idempotent 재사용**: digest 가 기존 게시본과 같으면 재게시하지 않고 기존 version/as_of 를 `ConsumedSource` 에 담는다(재게시하면 앞선 in-flight facts 가 헛되이 stale).
6. 게시 순서 = 출처 전부 → `publish_decision_facts` → (S3 의 prepare).
7. CV 증거 채널은 **진입 시 None + 요청 token 정확 대조**. 불일치·None 이면 facts 를 만들지 않는다.
8. `facts.position_multiplier` = `signal.signal.metadata` 의 **ATR skip 이전** 값(`event.signal` 이 None 이면 1.0). `facts.atr_pct` 는 risk 모드에서만 non-None.
9. `llm_verdict` 어휘: `approved` / `rejected_soft` / `not_required` / `skipped_no_manager` / `skipped_bull` / `skipped_low_score` / `fail_open_quota` / `fail_open_error`. 반환 bool 불변.
10. overlay 예외는 `applied_rule_ids` 의 `overlay_calendar_unavailable` / `overlay_volatility_unavailable` / `overlay_conviction_unavailable` 로 표현, 배율 필드는 1.0.
11. `applied_rule_ids` 는 penalties 한국어 문자열에서 **유도**한다(penalties 리스트·문자열·`_HARD_BLOCK_TAGS` 매칭은 불변 — 누적 cap 판정이 그 문자열에 의존). 유도 표는 `qualification.py` 에 두고 시험으로 고정.
12. builder 가 `decided_at`(aware KST)으로 재계산한 CV 시간 구간과 CV 가 보고한 penalties 태그가 다르면 facts 를 만들지 않는다(`decision_clock_disagreement`).

13. **증거 캡처 순서(S2-5, wave A 리뷰에서 추가).** 증거 채널은 그 자체로 fail-closed 가 아니다 — 안전성은 **읽는 순서**가 정한다. 어댑터는 (a) 자기 `llm_second_check` await 가 끝난 **직후**(LLM 을 부르지 않는 경로는 validate 직후) (b) `last_decision['token']` 이 자기 token 과 정확히 같은지 대조하고 (c) `last_decision` 과 `last_llm_reason` 을 **추가 await 없이 함께 복사**한다. 불일치·None 이면 게시 0. token 대조만으로 충분하다고 일반화하지 않는다: Codex 재현에서 "B validate → 지연된 A 의 `_llm()`" 은 **token B + A 의 사유**를 만든다. 현재 매수 경로는 LLM 복귀부터 사이징까지 await 가 없어 이 캡처 지점을 확보할 수 있다(S2-5 worker 가 다시 확인).
14. **사이징 입력은 현재 요청의 성공한 사이징 직후에만 읽는다(S2-5).** `getattr(rm, '_last_sizing_inputs', None)` 은 속성 부재만 막는다 — 현재 요청이 사이징 전에 끝났는데 이전 요청의 성공값을 읽는 경우는 막지 못하므로, 어댑터는 자기 요청의 `_calculate_position_size` 가 양의 수량을 돌려준 **바로 그 지점**에서 복사한다.
15. **0 배율·hybrid 는 facts 를 만들지 않는다(S2-4).** legacy 는 0.0 배율을 처리하지만 S1 DTO 는 배율을 `positive=True` 로 검사해 거부하고 hybrid 는 재유도를 거부한다. builder 는 이를 `0 or 1.0` 같은 보정으로 넘기지 말고 facts 미생성(자동 BUY 게시 0)으로 끝낸다 — 사유 코드를 남긴다.

### 출처별 전략

| 출처 | digest | 게시 | final stale 실효성 |
|---|---|---|---|
| regime | `{'effective_regime': 문자열}` | 어댑터·결정 시점·idempotent | **있음(유일)** — owner 가 final 에서 재유도 |
| panel_outlook (보너스가 실제로 붙었을 때만) | `{created_at, 해당 symbol conviction, 계산된 bonus}` | 어댑터·결정 시점, `as_of`=CV 가 그 스냅샷을 읽은 시각 | **없음 — replay 구속 전용.** `expires_at <= panel_loaded_at + 6h` 로 창 축소 |
| trade_memory (memory_adj != 0 일 때만) | `{활성 원칙 rule, score_delta, 적용된 memory_adj}` | 어댑터·결정 시점 | **없음 — replay 구속 전용.** 장중 불변, 요일 의존항은 당일 만료로 흡수 |
| expert_orchestrator·sector_council | — | 게시하지 않음 | 판정 기여 0. `experts.shadow_mode` 는 config_version 에 실어 끄면 config 변화로 검출 |
| portfolio.sector·`_exited_today` | — | 게시하지 않음 | owner snapshot 이 매 `_evaluate` 에서 재독 |
| trade_wiki | — | 게시하지 않음 | `validate()` 입력이 아님 |
| 설정 | `config_version` 문자열 1개 | 출처가 아니라 `facts.config_version` | S2 에서는 자기 일관성뿐, S3 에서 실효 |

## 4. 하위 단계 (각 단계 RED→GREEN→독립 재검증→coordinator 통합)

공통: 기준선 시험 파일(`test_execution_sizing_characterization.py`·`test_execution_decision_facts.py`·`test_t11_money_path_baseline.py`·`test_entry_risk_lifecycle.py`·`test_t11_entry_plan.py`·`test_t10_repro_a.py` 등) **수정 0줄·전건 통과**가 인수 조건. 새 시험은 각자 `synthetic_home` autouse 선언, 시계 주입, 벽시계 의존 0. **변이 kill 이 인수 조건**(나열한 변이 각각 ≥1건 실패) + 독립 재검증자가 자기 변이 3종 추가.

### S2-1 — CV 특성화 기준선 + 증거 채널 (wave A)
- 제품 `src/core/cross_validator.py`(가산 ~20줄) / 시험 `tests/test_cross_validator_characterization.py`(신규)
- **순서가 핵심:** 특성화 C1~C7 을 **먼저** 세운다(착수 시 GREEN 이어야 정상 — 기준선 고정, RED 로 세지 않음). 그 다음 증거 채널.
- 고정 인터페이스: `validate(symbol, side, strategy, score, metadata, market_regime='neutral', count_stats=True, request_token: str | None = None) -> tuple[bool, float, str]`(새 kwarg 는 맨 뒤·기본 None) · `last_decision: dict | None`(진입부 무조건 None, 통과 return 직전 1곳에서만 대입; 키 `token, symbol, side, strategy, regime, penalties(tuple), cap_applied, original, adjusted, memory_adj, panel(None|{created_at, conviction, bonus, loaded_at}), now_hm`) · `last_llm_reason: str | None`(계약 9 의 어휘)
- 특성화: C1 시간 3구간(09:29 차단 / 09:45 −8 / 12:45 +5, 13:00 포함·13:01 미포함) · C2 batch_signal·core_holding·strategic_swing 면제 · C3 지표결손 개수×STEP·cap · C4 누적 cap 15 와 hard-block 태그 3개 면제 · C5 `MIN_PASS_SCORE` 50 경계 · C6 side!='buy' 무검증 통과 · C7 market='US' 인스턴스(규칙2 미적용·규칙3 momentum 만)
- 행동 RED: R1 차단 verdict 뒤 `last_decision is None` · R2 실거래 validate(token='t1') 뒤 shadow validate(`count_stats=False`)가 끼면 `last_decision` 이 내 token 이 아님 → 어댑터가 facts 를 못 만든다 · R3 LLM 일일 한도 fail-open 과 실제 approve 가 `last_llm_reason` 으로 구분 · R4 token 유무로 `(bool,float,str)`·`_stats` 증분 동일
- 변이: 진입부 None 초기화 삭제→R2 · `last_llm_reason` 전부 'approved'→R3 · `_MIN_PASS_SCORE` 50→49 / `TOTAL_PENALTY_CAP` 15→16 / early_session 8→7 / 지표결손 cap 변경→C1·C3·C4·C5 · `_HARD_BLOCK_TAGS` 에서 '추격매수' 제거→C4 · 09:00~09:29 차단을 09:25 로→C1 · 12:30~13:00 의 `<=1300` 을 `<1300`→C1
- **착수 전 실측:** `cross_validator.py:258` 지역 재임포트 때문에 모듈 monkeypatch 가 듣지 않는다. `datetime` 모듈 속성 교체(`monkeypatch.setattr(datetime_module, 'datetime', Frozen)`) 등 동결 수단을 먼저 실측하고, 불가하면 CV 시그니처를 임의로 바꾸지 말고 보고. US 호출(`us_scheduler.py:1083-1104`)이 keyword 인지 확인.
- 금지: 감점 산식·임계값·penalties 문자열·반환 tuple·규칙11/12 파일 쓰기 변경, 기존 시험 파일 수정.

### S2-2 — 사이징 입력 반출 + overlay fail-open 표식 (wave A)
- 제품 `src/core/engine.py` — **`RiskManager._calculate_position_size` 내부로만 한정**(~15줄) / 시험 `tests/test_execution_sizing_inputs_export.py`(신규)
- 고정 인터페이스: `self._last_sizing_inputs: dict | None`(진입부 None, 성공 tail 에서만 대입; 키 `base_pct, strategy_allocation_pct(None 가능), min_position_value(Decimal), strength_multiplier, position_multiplier(ATR skip 이전), calendar_multiplier, volatility_multiplier, conviction_multiplier, atr_pct(risk 모드만), stop_pct, stop_source, stop_crash_capped, hybrid_enabled, overlay_status{'calendar'|'volatility'|'conviction': 'applied'|'unavailable'}`)
- 행동 RED: R5 nominal·risk 표본의 반출값이 특성화 시험이 못 박은 값과 같다 · R6 calendar provider 예외 → `overlay_status['calendar']=='unavailable'`·배율 1.0·수량은 provider 가 1.0 을 준 경우와 동일 · R7 조기 return 경로는 None(직전 값 잔류 금지) · R8 nominal 모드에서 metadata 에 atr_pct 가 있어도 None · R9 `event.signal is None` 이면 position_multiplier 1.0 · R10 사이징 전후 event/signal metadata 키 집합 불변
- 변이: ATR skip 이후 값 기록→R5 · overlay_status 제거→R6 · 진입부 None 제거→R7 · `event.metadata` 에서 읽기→R9 · atr_pct 모드 무관 기록→R8 · min_position_value/base_pct 를 config 폴백으로 대체→R5
- 금지: provider 재호출(특성화 시험의 호출 순서·횟수 단언이 기계적 증명), 수량·분기·overlay 순서 변경, engine.py 의 다른 구간 수정.

### S2-3 — regime final 재유도 대조 (wave B)
- 제품 `src/execution/safety/commands.py`(~25줄) / 시험 `tests/test_execution_regime_recheck.py`(신규)
- 고정 인터페이스: `RequestBoundCommands.read_qualification_source(name) -> dict | None`(deepcopy) · 사유 코드 `stale_regime_decision` · `qualification.regime_digest` 와 같은 digest 식을 쓴다(S2-4 와의 접점 — 식은 계약 2 로 고정: `digest(canonical({'effective_regime': 값}))`)
- 행동 RED: R11 'sideways' 로 게시·소비 후 `intraday_crash_level` 변경으로 effective_regime 이 바뀌면 final NOT_SENT·POST0 · R12 regime_policy 커밋이 있어도 문자열이 그대로면 POST1(과도 stale 없음) · R13 무관 fill 로 owner.version 만 증가→POST1 · R14 같은 digest 는 재게시 없이 기존 version/as_of 재사용이 `_consumed_sources` 통과 · R15 `regime_policy` 없는 state 에서 'regime' 소비 facts 거부 · R16 `read_qualification_source` 반환값 변형이 게시본에 영향 없음
- 변이: 재유도 대조 삭제→R11 · digest 를 regime_policy 루트 전체로→R12 · `effective_regime(state, facts.decided_at)`(현재 now 대신)→R11 · regime_policy 부재 시 통과→R15 · deepcopy 제거→R16
- 금지: `decisions.py`(S1 승인 DTO) 수정, `EffectiveRiskPolicy`·`PolicyContext` 필드 추가, `_consumed_sources` 기존 검사 약화, 기존 시험 파일 수정.

### S2-4 — 순수 builder 모듈 (wave B)
- 제품 `src/execution/safety/qualification.py`(신규 ~200줄, I/O·await·`datetime.now()` 금지, now 는 인자) / 시험 `tests/test_execution_qualification_builder.py`(신규)
- 고정 인터페이스: `PendingSource(name, as_of, digest)`(frozen) · `RULE_IDS: tuple[tuple[str, str], ...]`(penalties substring → 안정 id) · `config_version(*, validator, llm, sizing, stops, experts) -> str`(매 호출 재계산) · `entry_expires_at(decided_at, *, strategy, panel_loaded_at=None) -> datetime` · `regime_digest(regime) -> str` · `build_decision_facts(*, intent_id, symbol, side, strategy, origin, sector, cv_decision, llm_reason, sizing_inputs, config_version, regime_used, regime_row, decided_at) -> tuple[EntryDecisionFacts, tuple[PendingSource, ...]]`(입력은 S2-1·S2-2 가 고정한 dict 키)
- 행동 RED: R17 expires_at 5건(09:10→09:30, 09:45→10:30, 11:00→12:30, 12:45→13:01, 15:25→당일 말; KST 자정 불초과) · R18 sepa_trend 14:29→14:30 · R19 panel 기여 시 `<= panel_loaded_at+6h` · R20 CV 가 '장초반 변동성'을 보고했는데 decided_at 이 11:00 → `decision_clock_disagreement` · R21 panel digest 는 해당 symbol 의 3값만(다른 종목 추천 변화에 불변) · R22 memory_adj==0·panel 미기여면 sources 는 regime 뿐 · R23 sector_council·exited_today·portfolio.sector·trade_wiki·expert 미인용 · R24 overlay 예외→`overlay_*_unavailable` · R25 `fail_open_quota` 와 `approved` 구분 · R26 config_version 이 base_pct 표/validator 임계값/STRATEGY_EXIT_PARAMS/hybrid.enabled/experts.shadow_mode 각각의 변화에 반응(대조: `engine._entry_risk_config_hash` 는 base_pct 표 변화에 무반응임을 같은 시험에서 고정) · R27 applied_rule_ids 중복 없음 · R28 regime_row 없거나 digest 불일치→facts 미생성
- 변이: CV 다음 경계 항 제거→R17 · SEPA 14:30 제거→R18 · panel 6h 제거→R19 · 시계 교차검증 제거→R20 · panel digest 에 전체 목록→R21 · "기여한 출처만" 술어 제거→R22·R23 · config_version 입력에서 base_pct 표/shadow_mode 제거→R26 · llm_verdict 2값 축약→R25 · regime digest 대조 제거→R28 · 12:30~13:00 경계를 13:00 으로→R17
- 금지: CV 임계값 재계산(기록값만), `decisions.py` 수정, engine·cross_validator 접촉.

### S2-5 — engine 어댑터 배선 (wave C, S2-1~4 통합 뒤)
- 제품 `src/core/engine.py` — **qualification 어댑터 구간만**(CV 블록 종료 후 ~ 사이징 수량 확정 직후, ~40줄; S2-2 의 `_calculate_position_size` 내부·S3 의 on_signal 종착부·`_process_event` 분기와 겹치지 않는다) / 시험 `tests/test_execution_qualification_publishers.py`(신규)
- 어댑터 계약: runtime 이 붙어 있을 때만 실행(미설치면 0줄 실행). `decided_at` 은 `runtime._now()` 로 **한 번** 찍어 builder·게시에 같은 값을 쓴다. 게시 순서 = 출처 전부 → `publish_decision_facts`. `intent_id` 는 S3 gateway 가 발급(S2 에서는 어댑터 인자). `facts.strategy` 는 request.strategy 와 같아야 하므로 'unknown' 폴백 전략은 facts 를 만들지 않는다.
- 행동 RED: R29 end-to-end(실제 `RequestBoundCommands` + 합성 startup)로 게시된 facts 가 `publish_decision_facts`·`_decision_facts` 통과 · R30 게시 순서 역전→`stale_qualification_source` · R31 같은 패널로 두 종목 연속 판단해도 panel version 은 한 번만 오르고 앞 요청이 stale 이 되지 않음 · R32 runtime 미설치에서 같은 Order·게시 호출 0 · R33 CV trace token 이 내 요청과 다르면 게시 0 · R34 `_last_sizing_inputs` 가 None 이면 게시 0 · R35 SELL·USER·SAFE_ASSET 게시 0
- 변이: token 대조 제거→R33 · idempotent 재사용 제거→R31 · 게시 순서 역전→R30 · runtime 미설치 가드 제거→R32 · None 검사 제거→R34 · decided_at 을 두 번 찍음→as_of<=decided_at 경계 표본
- 금지: facts 를 **소비**하는 제품 호출자(gateway·prepare·dispatch) 생성(S3), `engine.py:526-533` SIGNAL 폐기 분기 수정(S3), on_signal 판정·in-place 변형 8지점 수정, 기존 시험 파일 수정.

#### S2-5 범위 정정 (coordinator, wave B 통합 뒤 — 위 S2-5 절보다 우선한다)

위 초안은 "engine 어댑터가 on_signal 안에서 게시까지" 였으나 세 가지 때문에 성립하지 않는다: ① `intent_id` 는 S3 gateway 가 발급하므로 on_signal 안에서는 모른다 ② `config_version` 의 `stops` 축(`run_trader.py:498 _strategy_exit_params`·exit_manager 표)은 RiskManager 가 들고 있지 않다 — 조립할 수 있는 곳은 S3 의 factory 다 ③ 게시 호출을 둘 자리(레거시 게이트 G15~G17 뒤, Order 생성 자리)는 on_signal **종착부**로 S3 소유다. 따라서 S2-5 는 **"증거 캡처" + "게시 함수"** 두 부품까지이고, 둘을 잇는 호출은 S3 gateway 가 한다.

- **부품 A — 게시 함수(순수 async, engine 무의존):** `src/execution/safety/qualification_publisher.py`(신규).
  `QualificationEvidence`(frozen: `token, symbol, side, strategy, origin, sector, cv_decision, llm_reason, sizing_inputs, regime_used`) ·
  `async def publish_qualification(commands, evidence, *, intent_id, config_version, decided_at) -> EntryDecisionFacts` — 순서 고정(계약 6): ① regime 을 `read_qualification_source('regime')` 로 읽어 **digest 가 같고 당일이면 재사용**, 아니면 `publish_qualification_source` ② `build_decision_facts(..., regime_row=...)` ③ `PendingSource` 각각을 같은 idempotent 규칙으로 게시해 `ConsumedSource` 로 만들고 `dataclasses.replace(facts, sources=...)` ④ `publish_decision_facts`. `QualificationRefused`·`CommandValidationError` 는 삼키지 않고 그대로 올린다(게시 0).
  `commands.py` 의 regime digest 는 `qualification.regime_digest` 와 **같은 값**이어야 한다 — 동등성 시험으로 고정한다(commands.py 는 S2-5 에서 수정하지 않는다).
- **부품 B — on_signal 의 증거 캡처(`src/core/engine.py`, runtime 이 붙어 있을 때만):** token(`uuid4().hex`) 발급 → `validate(..., request_token=token)` → **자기 `llm_second_check` await 직후**(LLM 을 부르지 않는 경로는 validate 직후) `last_decision['token'] == token` 을 대조하고 `last_decision`·`last_llm_reason` 을 **추가 await 없이 함께 복사**(계약 13) → `_calculate_position_size` 가 양수를 돌려준 **바로 그 지점**에서 `getattr(self, '_last_sizing_inputs', None)` 복사(계약 14) → sector 조회 뒤 `QualificationEvidence` 를 만들어 `self._last_qualification_evidence` 에 둔다(on_signal 진입부에서 None 으로 초기화, 불일치·None·조기 return 이면 None 유지). **게시 호출·intent_id·config_version 은 넣지 않는다.** runtime 미설치면 이 분기 전체가 실행되지 않는다(legacy 0줄 실행). 허용 구간: CV 호출부의 kwarg 1개, LLM 블록 직후 복사, 사이징 직후 복사, sector 조회 직후 조립, 진입부 초기화 — on_signal 의 판정·in-place 변형 8지점·종착부(G15 이후)·`_process_event` 는 건드리지 않는다.
- **시험(`tests/test_execution_qualification_publishers.py`):** R29 실제 `CrossStrategyValidator`·실제 `RiskManager._calculate_position_size` 가 만든 증거 → `publish_qualification` → 실제 `RequestBoundCommands` 의 `publish_decision_facts`·prepare(`_decision_facts`) 통과(합성 startup 위) · R30 게시 순서 역전 → `stale_qualification_source` · R31 같은 패널로 두 종목 연속 → panel version 1회 상승·앞 요청 stale 아님 · R32 runtime 미설치 on_signal → 같은 Order·`_last_qualification_evidence` 미생성·게시 호출 0 · R33 validate 와 LLM 복귀 사이에 shadow validate 가 끼면 증거 None · R33b "B validate → 지연된 A 의 `_llm()`" 재현에서 A 의 증거에 B 의 token/사유가 섞이지 않는다 · R34 사이징이 0/조기 return 이면 증거 None(이전 요청 값 잔류 금지) · R35 SELL 신호는 증거 None · R36 `regime_digest` 동등성(commands 의 재유도 digest 와 builder 의 digest) · R37 0 배율·hybrid·SEPA 14:30 이후 → `QualificationRefused` 가 그대로 올라오고 게시 0.
- **변이:** token 대조 제거→R33 · decision 과 reason 을 서로 다른 시점에 복사(사이에 await)→R33b · idempotent 재사용 제거→R31 · 게시 순서 역전→R30 · runtime 미설치 가드 제거→R32 · 사이징 입력을 on_signal 진입 시점에 읽기→R34 · `replace(facts, sources=...)` 누락(pending 미인용)→R29 의 sources 단언.
- **S3 로 넘기는 것:** gateway 가 `_last_qualification_evidence`(또는 같은 호출 안의 지역값)를 받아 `intent_id` 발급·`config_version` 조립(factory 가 validator/llm/sizing 표/stops/experts 5축을 모은다)·`publish_qualification` 호출·prepare→ORDER→dispatch. 레거시 게이트 G15~G17 을 gateway 앞에 둘지, 증거 속성 대신 on_signal 종착부에서 지역값을 직접 넘길지는 S3 Plan 이 정한다.

## 5. legacy/US 불변의 증명

- 기존 시험 수정 0줄·전건 통과(특히 overlay 호출 순서·횟수 단언 — provider 재호출 금지의 기계적 증명).
- 새로 세우는 증명: CV 특성화 C1~C7(현재 증거 0) · 증거 채널 무부작용 R4 · runtime 미설치 0줄 실행 R32 + metadata 키 집합 불변 R10 · US: 통합 diff 에 `us_scheduler.py` 없음 + 새 kwarg 기본 None + C7.
- S2 전체 통합 뒤 전체 suite UTC→KST 단독 직렬.

## 6. S2 에서 하지 않는 것 (→ S3 이후)

hybrid policy 대조(S1 잔여 7, **S3 인수 조건**) · SIGNAL→gateway·intent_id 발급·prepare→ORDER→dispatch · PolicyContext 제품 publisher 와 config_version 실제 게시 · hybrid 사이징 재현(계속 명시 거부) · expert/sector_council 의 소비 출처화(shadow 해제 시 별도 작업, `expires_at <= min(valid_until)`) · US facts 계약 · facts/출처 정리 writer(S1 잔여 2) · `get_available_cash()` 등가 대조(S1 잔여 3) · sector 조회 실패가 None 으로 뭉개져 섹터 한도를 "제한 없음"으로 만드는 fail-open(`run_trader.py:1846-1848`; S2 는 `sector_lookup_failed` 로 사실만 기록) · `_dispatch` 의 `claim_not_available` 뭉개기(S1 잔여 8) · on_signal 상대 쿨다운(30초/300초/90·600초)의 facts 재현(S3 가 legacy 게이트를 먼저 통과시키는 순서로 해결) · runtime attach 시 legacy `_reserved_*`·`_pending_*` 가 비어 legacy 사이징이 owner 재유도보다 커지는 문제(S3 배선 결정).

## 7. 위험과 미확인 (착수하는 worker 가 먼저 확인할 것)

- 과도 stale(출처 객체 통째 해시 → 정상 주문의 조용한 거부, `_dispatch` 가 사유를 뭉개 추적도 어렵다)과 과소 stale(`days_old`·weekday 의존값 — digest 에 **계산된 결과값**을 넣어야 검출) 사이의 선택이 digest 설계의 전부다.
- `expires_at` 을 상한으로 두면 경계 직전(10:29:59) 판단이 1초 만에 만료된다(fail-closed 방향, 현금 고갈로 발생률 측정 불가).
- stop 3튜플은 owner 가 현재 ExitManager 상태로 재해석해 완전 일치를 요구한다(`commands.py:239-241`) — 판단~final 사이 급락 레벨이 바뀌면 `decision_stop_changed`. owner 의 stop_resolver 가 engine 과 같은 ExitManager 인스턴스인지는 S3 에서 확인.
- `adapter.regime` 은 owner 가 ready 가 아니면 None 이 아니라 **예외를 전파**한다(`market_regime.py:318`).
- 미확인(정적 대조에서 열지 않은 것): kernel 본문 · `_layer3` 수명주기와 expert_panel 갱신 스케줄(조사 인용) · `runtime._now()` 구현 · 팀심의 gate_checker 의 주기와 `count_stats=False` 여부 · `run_trader.py` 의 KR CV 생성부(validator 튜닝 kwargs — `config/default.yml` 에 top-level `validator:` 없음, 운영 튜닝값의 실제 경로 미확정 → **S2-4 worker 가 config_version 입력 정의 전에 확인**. coordinator 관측 단서: `config/default.yml:161` 부근에 **중첩된** `validator:` 블록(`min_pass_score: 50`, `missing_indicator_penalty_step/cap`, `llm_daily_max`, `rule_penalties.early_session: 8`·`sepa_chase: 10` …)이 있다 — top-level 이 아니라 상위 섹션 아래이므로 어느 config 객체로 읽혀 CV 생성자에 들어가는지 추적할 것) · `STRATEGY_EXIT_PARAMS` 정의 위치 · `INTRADAY_CRASH_PARAMS` 갱신 주기.

### wave B 에서 확정된 사실 (S2-3·S2-4 worker 의 착수 전 확인, `3c51117` 기준)

- **`cap_regime_by_intraday_risk` 는 bull 계열만 강등한다**(`src/core/market_regime.py:143-151`, `_BULL_DEMOTION={'bull':'sideways','trending_bull':'neutral'}`). 장중 급락으로 유효 레짐이 실제로 바뀌는 표본은 `mid_regime='bull'` 이어야 한다 — 'sideways' 기준선에서는 급락 레벨을 올려도 문자열이 그대로다.
- **validator 튜닝값의 실제 경로:** `config/default.yml:161` 의 중첩 `validator:`(kr 섹션 아래) → `scripts/run_trader.py:839` `_validator_cfg = kr_cfg.get("validator") or self.config.get("validator") or {}` → `run_trader.py:848` `RiskManager(..., validator_config=_validator_cfg)` → `engine.py:1339 _vcfg` → `engine.py:1355 CrossStrategyValidator(min_pass_score, missing_indicator_penalty_step/cap, llm_daily_max, rule_penalties)` 와 `engine.py:1394-1396 _LLM_CHECK_MIN/_LLM_BYPASS_AT/_LLM_REJECT_SIZE_MULT`. US 인스턴스는 `run_trader.py:1489` 별도.
- **`STRATEGY_EXIT_PARAMS` 라는 제품 상수는 없다**(`src/`·`scripts/` grep 0건 — 시험 상수일 뿐). 같은 자리의 제품 값은 `scripts/run_trader.py:498 self._strategy_exit_params`(전략별 stop_loss_pct 등, `config/default.yml` kr.strategies 에서 조립)이며 `run_trader.py:858-860 make_entry_stop_resolver(self.exit_manager, self._strategy_exit_params)` 로 위험 사이징 분모에 연결된다. 레짐/급락 표는 `src/strategies/exit_manager.py:79 REGIME_EXIT_PARAMS`·`:134 INTRADAY_CRASH_PARAMS`. config_version 의 `stops` 축은 이 값들을 받아야 한다.
- **base_pct 선택표는 코드 리터럴**이다(`engine.py:2616-2627 strategy_position_pct`: SEPA/STRATEGIC_SWING 25.0, VCP 15.0, RSI2 20.0, EARNINGS_DRIFT 20.0, THEME/GAP 15.0 …). 설정 파일이 아니므로 config_version 의 `sizing` 축에 이 표를 그대로 실어야 표 변경이 stale 로 잡힌다(`engine._entry_risk_config_hash` 는 이 표 변화에 무반응).
- **S2-4 의 반환 책임 분담(S2-5 가 따를 절차):** builder 는 게시 신원이 이미 확정된 'regime'(어댑터가 먼저 게시하고 `regime_row` 로 넘긴 게시본)만 `facts.sources` 에 담고, panel_outlook·trade_memory 는 `PendingSource` 로 돌려준다(version 은 게시해야 정해진다). 어댑터는 pending 을 게시 → `ConsumedSource` 로 만들어 `dataclasses.replace(facts, sources=...)` → `publish_decision_facts`.
- **trade_memory digest** 는 `{strategy, sector, memory_adj}` 다(계획 표의 `rule·score_delta` 는 S2-1 의 `last_decision` 에 없고 CV 수정이 금지라 얻을 수 없다 — replay 구속 전용이라 실효성 손실 없음).
- **S2-3 의 schema 의존:** "재유도는 현재 now" 의 시험 증명은 schema1 경로의 naive `.date()` 대조에 기대고 있다. schema3(horizon)는 양쪽을 KST 로 정규화해 같은 변이가 동치가 된다 → fixture 에 schema==1 경계 단언을 넣었다(`51a71e0`). **S3 인수 조건:** schema3 상태에서 "horizon 의 classified_at 당일 게이트가 현재 now 로 평가된다"는 축으로 이 계약을 다시 세운다.
- **게시는 막지 않는다:** stale 한 regime 을 인용한 facts 도 `publish_decision_facts` 는 통과한다 — 막히는 곳은 prepare 와 final 이다(계획대로). 게시 단계에서도 막을지는 S3 판단.

## 8. 역할·모델·한도

구현 = 요청 claude-opus-5/high 단일 writer(단계당 60분·허용 파일만·RED 먼저·변이 kill 보고) · 단계별 독립 재검증 = 구현자와 다른 실행의 opus/xhigh(변이 재현 + 자기 변이 3종) · wave 단위 Codex 교차 리뷰 = 요청 gpt-6-astra/xhigh(read-only, pytest 금지) · 통합·전체 suite·문서 = coordinator. pytest 에이전트 동시 ≤2, 전체 suite 는 단독 직렬. worker 는 harness 격리 worktree 에서 `git switch -c work/<단계> <base SHA>` 로 시작하고 기준선 시험을 첫 단계로 돌린다.
