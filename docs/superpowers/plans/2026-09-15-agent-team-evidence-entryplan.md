# T11. 에이전트 팀의 근거 정합성 개선과 조건부 진입계획(EntryPlan) 통합 (2026-09-15)

> 기준 main `3175732`. 개발 브랜치 `feature/t11-agent-team`. 상태 구분: **구현 완료 / 투자 성능 검증 완료 / 운영 승격 가능** 은 서로 다른 상태이며 이 계획은 첫 번째까지만 목표로 한다.

## 0. 목표·범위·금지

팀을 "BUY에 합의하는 시스템"에서 다음 체계로 바꾼다: 검증 가능한 자료로 근거를 평가하고, **매수 매력**과 **위험 허용**을 별도로 판단하며, **좋은 종목인지**와 **지금 이 가격·시점에 진입 가능한지**를 분리하고, 같은 **조건부 진입계획**을 팀 심의와 실행 검증이 공유하며, 기존 규칙 대비 에이전트·토론의 **추가 가치를 측정**할 도구를 갖춘다.

허용: 설계·구현·오프라인 검증·문서화·로컬 커밋(+ 사용자 지시에 따른 push·리뷰 PR). 금지: 운영 SSH/배포/재시작, 실주문·취소, 운영 설정·킬스위치·배분·사이징 변경, 팀 BUY 의 실주문 연결, 승인 없는 merge, 테스트의 외부 API·운영 자격증명·실계좌·운영 캐시 사용, 결과에 맞춘 임계값·목표가·대조군 사후 변경.

**새 판단 정책(TeamAssessment)과 EntryPlan 실행 검증기는 shadow 전용**이다: 기존 `TradeProposal/PMDecision/conviction` 경로와 주문 경로의 결과를 바꾸지 않는다. shadow 장애(예외·시간초과·저장 실패)는 돈 경로에 전파되지 않는다(예외 격리 + 테스트).

## 1. 현 코드 재확인 (main 3175732, 정찰 5영역 + 통합 담당 직접 독해)

| 지적 | 상태 | 코드 경로·근거 | 기존 작업 중복 | 이번 수정 범위 |
|---|---|---|---|---|
| A1 합의=확률: consensus True → `debate_adj=+20×1.0`, `conviction=0.5+0.4×1.0=0.90` | 현재도 존재 | `trader.py:66-69`, `researchers.py:335-340` | 없음 | 기존 산식은 **비교 기준으로 보존**. 신규 `TeamAssessment` 가 합의 수준·근거 품질·성공확률(미보정)을 분리 |
| A2 화면 사례(+10/+25/+52, 근거량 1.75, 종합 +28→+48, 0.90) | 현재도 존재(정찰 B 가 순수 함수로 재계산 일치) | `analysts.aggregate_score`, `types.freshness_decayed_confidence` | 없음 | 기준선 특성화 테스트로 고정 |
| B1 Bear ACCEPT(위험 허용)가 Bull 찬성과 합쳐져 만장일치 +20 | 현재도 존재(2026-08-03 CHANGELOG 에 의도 설계로 기록) | `researchers.py:250-252,335-340`, `BEAR_SYSTEM` | 재확인 | 신규 정책: `risk_acceptable` 별도 필드, 매수 매력 점수에 미가산 |
| B2 만장일치→사이징 부스트·PM override 조건 | 일부(경로 존재, 운영 `TEAM_CONVICTION=0`·`allow_pm_override=false` 로 잠김) | `team_conviction.py:84-106`, `engine.py:2567`, `portfolio_manager.py:62-64` | CLAUDE.md 기재 | 코드 경로 유지(정책 판단), 화면에 "합의 기반·확률 미보정" 표기 |
| B3 conviction 한 필드에 합의·품질·확률 혼재 | 현재도 존재 | `types.py:119-131`, `trader.py:63-80` | 없음 | `TeamAssessment` 분리 필드 |
| B4 R1 독립 판단 보존·변경 이유 | 일부(turns 에 R1/R2 stance 보존, 변경 이유 없음) | `researchers.py:246-306` | 없음 | R2 프롬프트에 변경 사유 구조화 줄, `DebateTurn.change_reason` |
| B5 판단 불능이 기권으로 남는가 | 일부(신규 매수 fail-closed, 보유 fail-open; docstring stale) | `researchers.py:18,309-334`, `trader.py:65-80` | 없음 | `TeamAssessment.abstained/abstain_reason` 명시, docstring 정정 |
| B6 LLM 이 가격·수량 계산 관여 | 재현되지 않음 | trader/pm 전체 결정론 | — | 없음 |
| B7 재현성 원장 params | 일부(`reasoning_effort:'low'` 하드코딩 vs 실제 minimal, seed/temperature 미기록, 프롬프트 버전 없음) | `researchers.py:268`, `reproducibility.py:56-89` | 없음 | 상수 참조·seed/temperature·`prompt_version` 기록 |
| C1 '검증 통과' +10 | 현재도 존재 | `analysts.py:110-116` | 없음 | 기존 score 유지(기준선), 신규 evidence 에서는 `risk_clear`≠`positive_basis` 분리 → 신규 정책 미가산 |
| C2 자료 부족·예외가 approved=True | 현재도 존재 | `stock_validator.py:48,144-157` | 없음 | `ValidationResult.validated/data_status` 추가(기존 `approved` 의미 불변) |
| C3 data_as_of 가 실제 시각 아님 | 일부(후보 경로는 08-02 수정, 보유 재평가 경로 `indicators_as_of` 미전달 → now) | `analysts.py:142,314,171`, `kr_scheduler.py:5764-5783` | 없음 | 보유 경로 `indicators_as_of` 전달, 신규 `observed_at` 은 모르면 None |
| C4 confidence=0 보고서가 유효 소스 수 포함 | 일부(`failed()` 는 제외, '정보 없음'(error 없음, conf 0)은 포함) | `analysts.py:399` | 없음 | `valid` 필터에 `confidence>0` (기존 경로 버그 수정, 특성화 테스트 분리) |
| C5 vol_ratio vs volume_ratio | 현재도 존재(거래량 +15 절대 미발동) | `technical.py:168,371` vs `analysts.py:200` | 없음 | 소비 키 정정 + 생산자→소비자 계약 테스트 |
| C6 같은 기사 재인용 | 일부(종목별 `get_symbol_sentiment` dedup 없음) | `news_curator.py:164` | 없음 | dedup 적용 + evidence `dedup_key` |
| C7 헤드라인만 사용, 한계 미표기 | 현재도 존재 | `news_curator.py:340` | 없음 | `limitations=["헤드라인 기반"]` 표기(프롬프트 본문 추가는 비용 판단 보류) |
| C8 근거 필드 인벤토리 | 필드 부재(출처·식별자·수집/관측 구분·회계기간·상태·유효기간) | `types.py:33-49` | T9 `DataPoint` 재사용 가능 | `EvidenceItem` 계약 |
| D1 TradeProposal 에 실행 조건 없음 | 현재도 존재(설계상 shadow) | `types.py:160-197` | 없음 | `TeamVerdict.assessment.entry_check` 로 계획 참조 |
| D2 PendingSignal 조건 완비 | 현재도 존재(정상) | `batch_analyzer.py:36-82` | 없음 | 확장 필드 추가(정본) |
| D3 조건이 Signal/주문까지 유지되지 않음 | 현재도 존재(게이트 통과 후 `Signal.price` 고정, 이후 await 다수 후 재검증 없음) | `batch_analyzer.py:1090-1097,1266-1291`, `engine.py:2040-2130` | 없음 | `Signal.metadata["entry_plan"]` 전달 + 주문 직전 shadow 검증기 |
| D4 대기 중 재검증 없음 | 재현되지 않음(CARRY 이월 6종 존재) | `batch_analyzer.py:85-107` | 2026-08-03 | 없음 |
| D5 시장가가 최대진입가를 보장 안 함 | 현재도 존재(구조적) | `engine.py:2019-2030`, `kis_kr.py:548-571` | 없음 | **운영 주문 방식 불변**, 지정가는 모의 연구(E)로만 |
| D6 팀 BUY 와 주문·체결 혼동 | 일부(office·텔레그램은 안내 있음, `/engine` 카드 없음) | `engine.js:186-191`, `engine.html:477-485` | 없음 | 실행 상태 5단계 표시 |
| D7 전략별 진입 조건 | 현재도 존재(VCP 만 breakout, 나머지 close) | `swing_screener.py:607`, `sepa_trend.py:125`, `gap_and_go.py:203` | 없음 | `setup` 별 필수 조건을 계획에 명시 |
| D8 손절·계획 위험 연결 | 이미 수정됨(T2/T3 entry_risk) | `engine.py:2641-2666` | PR #33~#38 | 재사용(`exit_policy_ref`) |
| E1 승인 BUY 가 CF 제외 | 현재도 존재(체결 교차확인 없음) | `counterfactual_tracker.py:107-109` | 없음 | 체결 없는 승인 BUY 를 `team_buy_unfilled` 로 추적 |
| E2 같은 날 같은 종목 덮어쓰기 | 현재도 존재(verdict 파일 last-write-wins) | `team.py:441` | 없음 | append-only 심의 원장 신설(파일은 대시보드 호환 유지) |
| E3 종가→종가 평가 | 현재도 존재(의도된 근사) | `counterfactual_tracker.py:205-213`, `gate_performance.py:129-158` | 없음 | 라벨 명확화, 승격 판정에 직접 사용 금지(E 러너는 체결 모델 명시) |
| E4 합의율·재현성·승률=성능 | 현재도 존재 | `shadow_lab.py:90-215`, `shadow_report.py:118-154` | 없음 | 라벨 분리("프로세스 품질", "P&L 미측정"), 승격 기준 변경 없음 |
| E5 A/B/C 러너 재사용 자원 | — | `backtest_strategies.py`(BTFeeCalculator/BTExitManager/live_policy), `ab_exit_policy.py`(manifest), `pending_signals_YYYY-MM-DD.json` 아카이브 | T5~T7 | E 러너 골격 재사용 |

## 2. 계약 (contract commit 에서 고정 — 구현자는 추측으로 바꾸지 않는다)

### 2.1 근거 계약 (`src/agents/types.py`)
- `EvidenceItem(source, metric, value, unit, observed_at, collected_at, period, status, kind, ref_id, valid_until, expiry_reason, dedup_key, note)` — `status ∈ {full, partial, insufficient, error}`, `kind ∈ {fact, interpretation, assumption}`. `observed_at` 은 **실제 관측/공표 시각을 모르면 None** (수집 시각·now 로 채우지 않는다). `from_datapoint(DataPoint, ...)` 로 T9 `DataPoint` 를 감싼다.
- `AnalystReport` 확장(기본값으로 하위 호환): `data_status="unknown"`, `evidence: List[EvidenceItem]`, `positive_basis: Optional[bool]`(긍정 근거 확인), `risk_clear: Optional[bool]`(위험 미발견), `observed_at: Optional[datetime]`(정직한 관측 시각), `limitations: List[str]`. 기존 `score/confidence/data_as_of` 의미는 기준선으로 **불변**.
- 계약 테스트: `technical.py` 생산 키 ⊆ `analysts.py` 소비 키(`vol_ratio`), 단위(%) 동일.

### 2.2 판단 계약 (`src/agents/types.py` `TeamAssessment`, `src/agents/judgment.py`)
- `TeamAssessment`: `merit_score/merit_status(sufficient|weak|insufficient|abstain)`, `risk_acceptable(True|False|None)`, `data_sufficiency(full|partial|insufficient)`, `entry_ready(True|False|None)`+`entry_check`, `consensus_level(unanimous|split|one_sided|failed)`, `evidence_quality{unique_sources, weight, dedup_removed, expired}`, `success_probability=None`+`calibration_status="uncalibrated"`, `abstained/abstain_reason`, `independent_votes(R1)/final_votes(R2)`, `change_reasons[]`, `stance_v2(buy_candidate|hold|abstain)`, `policy_version`.
- 규칙: 매수 매력은 evidence 기반(검증 통과·위험 미발견은 미가산, confidence=0·만료·error 제외, dedup_key 중복 1회만); Bear ACCEPT 는 `risk_acceptable` 에만; 만장일치는 `consensus_level` 에만(확률 아님); 자료 부족·파싱 실패·판단 불능은 `abstained=True`; `stance_v2=buy_candidate` 는 merit sufficient ∧ risk_acceptable ∧ data ≥ partial ∧ entry_ready True 일 때만. LLM 은 해석·반증만, 수치 계산은 코드.
- `DebateTurn.change_reason: Optional[dict]` — R2 에서 입장이 바뀌면 `{"kind": "new_evidence"|"prior_error"|"unrecorded", "text"}`. R2 프롬프트에 "입장을 바꾸면 첫 줄 뒤에 `변경사유: 새근거|이전해석오류 — …`" 지시 추가(바뀌지 않으면 무기록).
- 원장 params: `reasoning_effort=REASONING_EFFORT`, `seed`, `temperature`, `prompt_version="debate-v2-2026-09-15"`.
- 기존 `TradeProposal/PMDecision/conviction/team_conviction_multiplier` 산식·소비 경로 **불변**(기준선). `TeamVerdict.assessment: Optional[dict]` 로 shadow 결과 부착. 플래그 `TEAM_ASSESSMENT_V2` (기본 "1" = shadow 계산·기록, "0" = 미계산; 어느 쪽도 돈 경로 무영향).

### 2.3 EntryPlan 계약 (`src/core/batch_analyzer.PendingSignal` 확장, `src/execution/entry_plan.py`)
- `PendingSignal` 추가 필드(기본값·`from_dict` setdefault): `plan_id`, `plan_version`, `candidate_id`, `setup`(sepa_pullback|vcp_breakout|gap_vwap|rsi2_reversal|momentum|manual|""), `decided_at`, `inputs_ref{}`, `entry_band_low`(0=하한 없음), `trigger{type,level,satisfied,satisfied_at}`, `invalidation{stop_price,below_price,intraday_levels,expires_at}`, `required_inputs[]`, `missing_inputs[]`, `assumptions{fee_bps,slippage_bps,liquidity_ok,expected_fill_price}`, `exit_policy_ref`. 기존 `entry_price/max_entry_price/stop_price/target_price/expires_at/entry_mode/breakout_trigger` 는 그대로 정본.
- `PlanCheck(status allow|wait|reject, reasons[코드], expected_fill_price, cost_adjusted_rr, checked_at, quote_as_of, missing_inputs)`; 사유 코드: `PLAN_EXPIRED, PRICE_ABOVE_CAP, PRICE_BELOW_BAND, TRIGGER_NOT_MET, QUOTE_MISSING, QUOTE_STALE, INPUT_MISSING:<name>, INTRADAY_BLOCK:<level>, INVALIDATED:<cond>, RISK_BUDGET, SLOT_FULL, DAILY_LIMIT, COST_RR_LOW, CHECKER_ERROR`.
- `check_entry_plan(plan, quote, now, *, intraday_level=None, risk_ctx=None, setup_rules=None) -> PlanCheck` 순수 함수. setup 별 필수 조건: `vcp_breakout` 은 `trigger.level` 초과 필요, `gap_vwap` 은 `quote["vwap"]` 필요(없으면 `INPUT_MISSING:vwap` → wait), `sepa_pullback` 은 밴드 내 + 무효화 미해당. 계획이 없으면 검증하지 않는다(자동 생성 금지).
- 배선: PendingSignal 생성부가 `plan_id/setup/decided_at/trigger/exit_policy_ref` 를 채움; PendingSignal→Signal 변환이 `signal.metadata["entry_plan"]=plan.to_dict()` 를 실음; `engine.on_signal` 의 Order 생성 직전에 **shadow** `check_entry_plan` 실행 → `_log_sig(event_type="shadow_plan_check", metadata=PlanCheck)` 기록만(허용/차단 없음). 플래그 `ENTRY_PLAN_SHADOW`(기본 "1"; "0" 이면 검증기 미호출). 팀 심의는 후보 dict 의 `entry_plan` 으로 같은 함수를 호출해 `entry_ready` 산출.
- 운영 주문 방식(시장가) **불변**. 지정가·미체결·재시도는 E 러너의 모의 연구.

### 2.4 원장 계약 (`src/agents/team_ledger.py`)
- append-only JSONL `~/.cache/ai_trader/team_ledger/deliberations_YYYYMMDD.jsonl` (경로 상수 + 주입 가능). 행: `deliberation_id`(= sha256(symbol|date|slot|input_snapshot_hash)[:16] — 같은 입력 재시도는 같은 id → 소비자 dedup), `symbol, slot, decided_at, candidate_id, entry_plan_id, input_snapshot_hash, prompt_version, policy_version, model_calls[{role, round, model, provider}], reports(요약+evidence), independent_votes, final_votes, change_reasons, assessment, proposal(기존), decision(기존), execution_state`. 비밀·개인정보는 `entry_risk._strip_secrets` 패턴으로 마스킹. 저장 실패는 warning 만(돈 경로 무관).
- 기존 `team_verdicts/verdicts_YYYYMMDD.json`(대시보드·conviction 소비자)은 **호환 유지**(latest-per-symbol) + `deliberation_id` 필드 추가.
- `execution_state ∈ {candidate, waiting_trigger, shadow_ready, order_submitted, filled}` — 팀 판단·shadow 검증·실제 주문·체결을 분리 표시. `order_submitted/filled` 는 signal_events/trade_journal 의 실제 기록이 있을 때만.
- CF: 승인 BUY 중 당일 체결이 없는 건은 `team_buy_unfilled` 소스로 추적(자동 체결 가정 금지).

### 2.5 평가 도구 계약 (`scripts/team_policy_ab.py`, `results/team_policy_ab/`)
- 입력: 연구용 스냅샷 JSONL(후보별 `date, symbol, strategy, setup, plan(PendingSignal dict), evidence(AnalystReport dicts), votes{r1,r2}, prices(일봉 fixture 경로 또는 인라인)`). **실데이터가 없으면 합성 fixture 로 도구·판정 로직만 검증**하고 결과는 "미검증/보류".
- 정책: A 기존 규칙(스크리너 점수 순), B A+독립 근거 검토(`TeamAssessment` R1 만, 토론 없음), C B+토론(R2). 셋 다 **같은 후보군·같은 시점**에서 출발, BUY 사후 선별 금지.
- 실험 1(선정): 진입(09:01 시가)·청산(live_policy ExitManager 미러)·위험예산·비용(FeeCalculator) 고정, 정책만 비교. 실험 2(가격/시점): 후보·선정 고정, 기존 진입 vs EntryPlan 조건부 진입(트리거 접촉·밴드) — 일봉만으로 선후 불명확이면 **미체결 처리**(유리한 체결 생성 금지).
- 사전 등록(결과 전 고정): 주평가 = 포지션당 비용 차감 R(중앙값·평균); MDE = 중앙값 +0.10R(≈ 왕복 수수료 0.227%/5% SL 의 2배); 부평가 = 순손익·MDD·회전율·KODEX200 초과·MAE/MFE·체결률·대기시간·미체결 기회비용·기권/오류 비율·모델 비용·지연; 표본 요건 = 완결 포지션 ≥ 30/정책, 시간순 홀드아웃(마지막 1/3)에서 부호 유지; 누수 방지 = 판단 시각 이전 필드만, 종목-주 단위 클러스터, 겹치는 보유기간 독립 표본 취급 금지; 과거 LLM 재평가는 모델의 사후 지식 가능성을 한계로 명시. 승격 판정은 이 도구가 하지 않는다.

## 3. 소유권·분업 (동일 파일 동시 수정 금지)
- 통합(contract commit): `src/agents/types.py`, `src/core/batch_analyzer.py` PendingSignal 정의·from_dict, `src/execution/entry_plan.py`(PlanCheck + 검증기 서명·보수적 placeholder), `src/agents/team_ledger.py`(완성), 본 계획서.
- **A 근거**: `src/agents/analysts.py`, `src/signals/fundamentals/stock_validator.py`, `src/experts/news_curator.py`(dedup·한계 표기), `tests/test_t11_evidence.py`(+ 기준선 특성화). `technical.py` 는 읽기만.
- **B 판단**: `src/agents/judgment.py`(신규), `src/agents/researchers.py`, `src/agents/team.py`(assessment 부착·원장 호출·`entry_plan`/`slot` 수용), `src/agents/reproducibility.py`(prompt_version), `tests/test_t11_judgment.py`(+ 기준선 특성화: A2 사례 재계산).
- **C EntryPlan**: `src/execution/entry_plan.py` 본문, `src/core/batch_analyzer.py`(정의 외 로직: 생성부·변환부), `src/core/engine.py`(shadow 훅), `src/schedulers/kr_scheduler.py`(단독: 보유 `indicators_as_of`, 팀 후보 dict 에 `entry_plan`·`slot`·현재가 전달), `tests/test_t11_entry_plan.py`.
- **D 원장·화면·평가**: `src/analytics/counterfactual_tracker.py`, `src/dashboard/kr_api.py`·`office_api.py`·`static/js/engine.js`·`templates/engine.html`·`templates/office.html`, `scripts/shadow_report.py`, `src/analytics/shadow_lab.py`(라벨), `scripts/team_policy_ab.py`(신규), `tests/test_t11_ledger_eval.py`, 독립 재현 `tests/test_t11_repro.py`.
- 리뷰어 R-A/R-B/R-C/R-D(opus xhigh), 구현자≠리뷰어. 모델·effort: 구현 A/B/D sonnet high, C(돈 경로 훅) opus high, 리뷰 opus xhigh, 통합 최종 리뷰 opus xhigh.

## 4. 인수 조건 → 테스트 소유
| # | 조건 | 소유 |
|---|---|---|
| 1 | 근거 없는 검증 통과가 신규 정책에서 긍정 근거로 승격되지 않음 | A(evidence) + B(merit) |
| 2 | confidence=0·결측·오류·만료가 유효 소스를 부풀리지 않음 | A |
| 3 | 실제 원자료 시각 보존, 수집시각으로 신선도 세탁 금지 | A + C(보유 `indicators_as_of`) |
| 4 | 거래량 키·단위 계약 일치 | A |
| 5 | Bear 위험 허용만으로 매력·성공확률 상승 없음 | B |
| 6 | 토론 전후 판단·변경 이유 보존 | B |
| 7 | EntryPlan 조건이 직렬화·이벤트·대기 통과 후 유지 | C |
| 8 | 가격 상한 초과·만료·필수 결측은 shadow 에서 wait/reject | C |
| 9 | 플래그 off 시 돈 경로 결과 = 기준선 | C(+D 재현) |
| 10 | shadow 실패·시간초과·저장 오류가 돈 경로를 중단시키지 않음 | B/C/D |
| 11 | 같은 날 같은 종목 복수 판단·승인 BUY 누락 없이 추적 | D |
| 12 | 중복 이벤트·재시도가 원장·표본을 부풀리지 않음 | D |
| 13 | A/B/C 동일 후보·시점·위험·청산·비용 비교 | D |
| 14 | 검증 자료 없을 때 성공확률·개선 수치 생성 금지 | B/D |

## 5. 순서
1) contract commit → 2) A/B/C/D 병렬(격리 worktree, 실패 재현→최소 구현→통과) → 3) 브랜치별 독립 리뷰·수정 → 4) 통합 배선(통합 담당) → 5) D 독립 재현(14 조건)·통합 리뷰 → 6) 관련→격리 전체→비밀정보 검사, Codex 교차 리뷰(실패 시 미실행 기록) → 7) 문서(CHANGELOG·docs/agents/trading-team.md·system-overview·monitoring-checkpoints·CLAUDE.md)·로컬 커밋 → 8) push·리뷰 PR(머지·배포는 승인 대기).

## 6. 보류·질문 항목
- 뉴스 본문(원문) 프롬프트 포함은 LLM 비용 증가 → 한계 표기만 하고 보류.
- 지정가 주문 도입은 모의 연구까지만(운영 주문 방식 변경 없음).
- 실제 성능 검증은 연구용 스냅샷·원장 표본 확보 전까지 "미검증/보류".

- [ ] contract commit  - [ ] A  - [ ] B  - [ ] C  - [ ] D  - [ ] 통합·리뷰  - [ ] 문서·커밋  - [ ] PR
