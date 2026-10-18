# Live comparison preparation — 구현 분담·인터페이스 계약 (2026-10-13, 코디네이터 Claude)

인계 정본: [2026-10-13-live-comparison-claude-handoff.md](2026-10-13-live-comparison-claude-handoff.md). 이 문서는 Task1~6을 워커 3명에게 나누기 위한 **파일 소유권과 고정 인터페이스**다. 워커는 자기 소유 파일만 쓰고, 다른 소유자의 파일은 읽기만 한다. 공유 인터페이스를 바꿔야 하면 코디네이터에게 보고하고 멈춘다.

기준 SHA: main `d936518` (PR #189 포함). 운영 `161b394`·`KILL_SWITCH_KR`·월 $100 집계·10/14 08:55 파일럿은 불변. 오늘 SSH/systemctl/재시작/예약 설치/실주문/실제 KIS·유료 LLM 호출 없음. 테스트는 가짜 공급자·합성 자료·`tmp_path` 임시 원장만 쓰고 `Path.home` 을 `tmp_path` 로 패치한다(conftest 격리 가드).

## 소유권

| 워커 | Task | 소유(쓰기) 파일 |
|---|---|---|
| W1 | Task1 예산 보호·비용 복구 | `src/utils/llm_budget.py`(reserve 의 admission 훅만), `src/analytics/model_comparison_execution.py`, 신규 `src/analytics/model_comparison_recovery.py`, `tests/test_model_comparison_execution.py`, `tests/test_model_comparison_recovery.py`, `tests/test_llm_monthly_budget.py`, 신규 `tests/test_model_comparison_admission_budget.py`, `docs/research/model-comparison-runner-2026-10-13.md` 의 "실패·복구 절차" 절 |
| W2 | Task2 입력·ID·출처 + Task3 작업자 | 신규 `src/analytics/model_comparison_input.py`, 신규 `src/analytics/model_comparison_worker.py`, `src/core/cross_validator.py`, `src/analytics/llm_decision_record.py`, `src/core/engine.py`(훅 1곳), `src/analytics/model_comparison_runner.py`, `scripts/run_trader.py`(기본 OFF 배선·종료), 신규 `scripts/dev/comparison_worker_dryrun.py`, `tests/test_model_comparison_runner.py`, `tests/test_llm_decision_record.py`, 신규 `tests/test_model_comparison_input.py`, 신규 `tests/test_model_comparison_worker.py`, 신규 `docs/research/model-comparison-live-input-2026-10-13.md` |
| W3 | Task4 장 상태·진입 조건 + Task5 실험 명세·일별 보고 | 신규 `src/analytics/model_comparison_admission.py`, `src/analytics/model_comparison_outcome.py`, 신규 `src/analytics/model_comparison_daily_report.py`, `scripts/report_model_comparison.py`, 신규 `config/experiments/R1-bull-reasoning-20261013.json`, 신규 `docs/research/model-comparison-experiment-R1-2026-10-13.md`, `tests/test_model_comparison_outcome.py`, 신규 `tests/test_model_comparison_admission.py`, 신규 `tests/test_model_comparison_daily_report.py` |
| 코디네이터 | Task6 | `CLAUDE.md`, `CHANGELOG.md`, `docs/README.md`, `docs/operations/monitoring-checkpoints.md`, 통합 E2E 시험 `tests/test_live_comparison_e2e.py`, 전체 verify·PR |

읽기 전용 참고: `src/utils/llm.py`(절약 단계 처리 1195~1250행), `src/utils/llm_budget_policy.py`, `src/utils/llm_budget_reconciliation.py`, `scripts/reconcile_llm_budget.py`, `src/analytics/model_comparison_sizing.py`, `src/analytics/decision_quote_join.py`, `src/analytics/kis_market_state.py`, `src/analytics/entry_observation_journal.py`, `src/core/entry_llm_context.py`.

## 고정 인터페이스

### I1. 예산 admission 훅 (W1 제공, W2 소비)

`MonthlyBudgetLedger.reserve(..., admission_check=None)`: `BEGIN IMMEDIATE` 안에서 snapshot·정책 관측 뒤, INSERT 전에 `admission_check(db, status, committed_units, units, now)` 를 호출한다. 검사는 `BudgetBlocked(reason)` 로 거부하고, 거부 시 트랜잭션은 COMMIT(관측 유지)·예약 없음. 훅은 공급자 호출·수량 재생·큐 작업을 하지 않는다.

`ComparisonExecutionPolicy` v2 필드 추가: `experiment_usd_ceiling: str`(Decimal 문자열, 실험 전체 상한 — 예약+정산+unknown 합산), `valid_from: str`·`valid_until: str`(KST 날짜 `YYYY-MM-DD`, 포함), `required_policy_mode: "normal"`. `SCHEMA_VERSION = "model-comparison-execution-v2"`. 기존 v1 저장 정책이 있으면 `comparison_policy_mismatch` 로 거부(이전 미지원, 리셋 탈출구 없음).

`ComparisonCallGuard` 거부 사유(고정 문자열): `comparison_policy_disabled`(원장 `policy_enabled=False`), `comparison_policy_mode_not_normal`, `comparison_economy_threshold`(이번 예약 포함 `(committed+units)*100 > limit*80`), `comparison_experiment_ceiling`, `comparison_experiment_expired`, 기존 `essential_budget_reserved`/`monthly_limit_exceeded` 유지. 실험 사용액 = `policy_attempts.task='model_comparison' AND role=experiment_id` 인 예약의 `reserved_units`(reserved/unknown) 또는 `actual_units`(settled) 합. 실험 ID를 바꿔도 **같은 원장의 모든 comparison 예약**은 월 집계에 그대로 남고, 새 ID 는 새 정책 저장이 거부되므로 우회가 아니다.

`guard.lookup(attempt_id)` 반환에 추가: `reconciled`(`{"actual_cost_usd": str, "reconciliation_id": int, "applied_at": str, "reviewer_ref": str, "evidence_sha256": str}` 또는 None), `original_estimated_cost_usd`(응답의 `api_cost_usd`), state 값에 `known_reconciled` 추가(대사로 settled 된 원래 unknown). 원래 `result` 는 불변.

`model_comparison_recovery.inspect_attempt(guard, attempt_id) -> dict`, `inspect_pair(guard, pair_id) -> dict`(manifest·requested_at·result 유무·각 attempt inspect·`complete: bool`), `restore_pair_result(guard, pair_id, result_row, *, manifest_json) -> dict`(원래 결과 행 + 저장된 manifest 일치 + 각 attempt_id 가 저장소에 존재·상태 일치할 때만 `UPDATE ... WHERE result IS NULL`; 아니면 `ValueError`; 현재 시각으로 completion 시각을 만들지 않음), `export_incomplete_pair(guard, pair_id) -> dict`(진단만).

### I2. 입력 봉투 (W2 제공, W3 소비 — 필드 이름 고정)

`comparison-input-envelope-v1`(`src/analytics/model_comparison_input.py`):

```
decision_id, signal_id, symbol, strategy, score, market_regime,
common_context, common_context_sha256,
role_controls: {bull: RoleControls 필드 dict, bear: ..., fallback_chain: [...], fallback_prompt_suffix, fallback_system},
neutral_sizing: 스냅샷 dict 또는 None,
producer: {run_id, pid, code_version, source_sha256, config_sha256, experiment_id|None},
input_available_at (KST aware ISO = 공통 입력 구성 시각), handed_off_at, received_at|None,
envelope_sha256 (handed_off_at/received_at/attestation 제외 전 필드),
attestation: {"kind": "in_process_nonce", "nonce_sha256": ...}  — 같은 프로세스의 생산자/소비자만 아는 난수의 해시. 파일에서 읽은 봉투는 `caller_supplied_not_attested`.
```

`decision_id` 는 `llm_second_check` 에서 공통 입력·역할 조건 확정 **직후, 공급자 await 전** 한 번 `new_id("dec")` 로 생성하고, 같은 값이 `decision_record`(새 선택 인자 `decision_id=`; 생략 시 기존처럼 새로 생성해 호환), 신호 메타 `llm_decision.decision_id`, 봉투, 비교 attempt/pair 까지 이어진다. 전달은 기본 OFF(`cross_validator._comparison_handoff is None`), `put_nowait` 비차단, 실패는 판단에 영향 없음. 실제 호출에 도달하지 않은 판단(`stage="unavailable"`)은 봉투를 만들지 않는다.

### I3. 비교 행 v2 와 평가 기준시각 (W2 제공, W3 소비)

`model_comparison_runner.VERSION = 'model-comparison-pair-v2'`. 행 필드 추가: `input_available_at`, `enqueued_at`(handed_off_at), `requested_at`(작업자 dispatch 시각, 기존 의미 유지), `evaluation_anchor ∈ {'input_available_at','requested_at'}`, `evaluation_at = row[evaluation_anchor] + horizon`. `pricing_basis='paired_live_shadow'` 는 `evaluation_anchor='input_available_at'` 과 봉투 attestation 통과(`live_provenance='in_process_attested'`)를 요구한다. v1 행(`requested_at` 기준)은 보고서가 그대로 해석하고 `evaluation_anchor='requested_at'`(legacy) 로 표기한다 — 과거 행의 의미를 바꾸지 않는다. 각 arm 에 `decision_available_at`(완료), `queue_delay_ms = requested_at - enqueued_at`, `provider_latency_ms`(attempt latency 합)를 분리 기록한다.

### I4. 작업자 (W2)

`ComparisonWorker(runner, *, max_queue=8, max_input_age_seconds=5.0, max_concurrent_pairs=1, shutdown_deadline_seconds=20, store=...)`: 전용 스레드+자체 이벤트 루프(운영 루프 비차단). `offer(envelope) -> 'accepted'|'rejected_full'|'rejected_disabled'|'rejected_stale'|'rejected_duplicate'|'rejected_unattested'`. 카운터 `offered/accepted/rejected_*/expired/started/finished/incomplete`. `stop(deadline)`: 수락분 drain, 시간 초과는 `incomplete` 로 기록. 재시작 시 큐의 미완료 작업을 자동 재실행하지 않는다(SQLite `comparison_pairs` 의 result NULL 행은 Task1 `inspect_pair` 로만 조회). 런처 배선은 `COMPARISON_WORKER=1` 과 실험 manifest 경로 환경변수가 **둘 다** 있을 때만, 기본 OFF. 서비스/타이머 설치 없음.

### I5. 진입 조건·장 상태·일별 보고 (W3)

- `model_comparison_admission.replay_admission(envelope_or_snapshot, verdict, replay) -> {gates: {name: {status: 'passed'|'failed'|'unknown', as_of, reason, replayable}}, admissible: True|False|None}` — 순수 함수. 재생 가능한 게이트(현금·최소금액·전략 한도·수량 0·위험 0.7% 클램프 — 스냅샷 `state`/`config` 로 재계산 가능한 것)만 passed/failed, 외부 상태(보유 수·pending·매수 중지·증권사 한도·쿨다운·당일 손실 게이트의 현재값)는 `unknown`. 엔진 주문 경로를 호출하지 않는다.
- `model_comparison_admission.classify_market_state(raw_market_state) -> {verdict: 'normal_continuous'|'not_normal'|'unknown', basis: [...]}` — 코드 의미는 저장소 산출물(`kis_market_state.py` 의 source_sha256 가이드) 또는 공식 1차 문서로만 확정, 출처 URL/날짜/해시를 코드 상수에 기록. 결측·빈 값·오래된 상태·다른 채널/세대·미지 코드·충돌은 `unknown`.
- `model_comparison_outcome.compare_policy_outcomes` 는 `policy.evaluation_anchor ∈ {'requested_at','input_available_at'}` 를 받고 anchor 시각 = `row[anchor]`. 나머지 검증 유지. `admission` 과 `market_state` 증거가 모두 passed/normal 인 arm 만 계산, 아니면 `net_delta_krw=None` 과 사유.
- `model_comparison_daily_report.build_daily_report(pairs_rows, *, day_kst, envelopes_offered, ...) -> dict`: 분모 `offered/eligible/sampled/complete/excluded`, 결측 사유 집계, 모델·추론, 대기/응답 지연, arm 별 응답 뒤 가격·수량, 원래/대사 비용, 조건부 경제성, 텔레그램 **미리보기 문자열**(발송 함수 없음).
- 실험 manifest `config/experiments/R1-bull-reasoning-20261013.json`: `enabled:false`, Bull `gpt-5-mini` minimal/400 ↔ low/400(지원 여부는 `src/utils/llm.py` `_OPENAI_EFFORTS` 로 확인), Bear `gemini-3.1-flash-lite-preview` 400, 폴백 체인 명시, `pairs_per_day:1`, `concurrent_pairs:1`, 최악 시도 수·USD 상한을 토폴로지(공통 Bear 1+빈 재시도 1, Bull 2arm×(1+1), 폴백 ≤2)로 계산해 기록, `valid_from/valid_until`, 선정·제외·중단 규칙, 소스 지문.

## 공통 규칙

- 커밋 메시지·PR 본문에 작성 도구 표기 금지. 비밀값·운영 경로 출력 금지. `.env`/토큰/캐시 접근 금지.
- `0/0.0/""` 를 `or` 기본값으로 덮지 않는다. 금액은 Decimal 문자열.
- 새 테스트는 `monkeypatch.setattr(Path, "home", lambda: tmp_path)` 로 격리하고 네트워크를 쓰지 않는다.
- 각 워커는 자기 파일의 관련 테스트와 `python -m py_compile` 을 돌리고 명령·rc·건수를 보고한다. 전체 verify 는 코디네이터가 통합 후 돈다.
- 구현≠검증≠병합≠배포≠수익성 입증. 근거 없는 항목은 unknown/미완료로 남긴다.

## 2차 수정 — Codex 독립 리뷰(PR #190, head `1e6feac`) 반례 7건 반영 계약

리뷰 지적은 코디네이터가 코드로 확정했다(작업자 offer 에 일/크기 제한·파일 기록 없음, 복구가 `error` 키로 검사 생략·행 수준 값 미검증, 폴백이 시도당 시한, 장 상태가 무효 최신 기록을 걸러낸 뒤 옛 정상값 선택, 경제성 근거의 스냅샷/시각 미대조, 비용 집계의 추정 우선·행 덮어쓰기). 분담: **W-A** #1 실행 제한·#2 복구 무결성·#3 폴백 체인 시한·#4 영속 저널, **W-B** #5 최신 장 상태 우선·#6 경제성 근거 일관성·#7 비용 집계. 코디네이터: 통합·E2E 를 실제 저널 산출물 기반으로 교체·전체 verify·Codex 재리뷰.

| 계약 | 값 |
|---|---|
| 정책 v2 추가 필드 | `pairs_per_day: int ≥1`, `max_common_context_bytes: int ≥1` (기본값 없음, 저장 정책 JSON 포함) |
| 호출 전 제한 | 러너 `_claim` 의 `BEGIN IMMEDIATE` 안에서 KST 당일 `comparison_pairs` 입장 수(`admission_day` 열) ≥ `pairs_per_day` → `BudgetBlocked('comparison_daily_pair_cap')`, 입력 바이트 초과 → `comparison_input_too_large`; 차단된 pair 는 `result={"schema_version":"model-comparison-pair-blocked-v1","reason",...}` 로 고정(다음날 자동 재실행 없음). 작업자 `offer()` 도 같은 조건을 전송 전에 검사(`rejected_daily_pair_cap`/`rejected_input_too_large`), 일 카운트는 영속 저장소에서 읽는다 |
| 폴백 체인 시한 | `PairSpec.fallback_chain_timeout_seconds`(필수) = 운영 `SINGLE_FALLBACK_TIMEOUT`; `_arm` 폴백 루프 전체를 하나의 `asyncio.timeout` 으로 감싼다. 봉투 `role_controls.fallback_chain_timeout_seconds`·manifest 키 동일 |
| 작업자 저널 | `journal_dir`(기본 `~/.cache/ai_trader/comparison_worker/`) 의 `offers-YYYY-MM.jsonl`·`incomplete-YYYY-MM.jsonl`(KST 월), 레코드 `comparison-offer-v1` `{kind:"offer", recorded_at, decision_id, status, reason, received_at, envelope}` / `{kind:"transition", decision_id, status, reason, at, pair_id}` / `{kind:"incomplete_result", recorded_at, pair_id, row_sha256, row}`. 운영 루프는 큐에 넣기만, 작업자 스레드가 쓴다. 읽기 `read_offer_journal(path, *, max_bytes)`·`read_incomplete_journal(path, *, max_bytes)` |
| 복구 | `restore_pair_result(guard, pair_id, result_row, *, manifest_json, evidence_path)`: 저장 응답 **전체 키** 일치(`error` 유무 무관), 파생 필드 재계산 일치, 경제성 플래그 강제, 행 sha256 이 incomplete 저널에 있어야 복원 |
| 장 상태 | 채널별 **최신 레코드 먼저 선택** → 그 레코드가 미자격/빈 값/다른 세대/시각 불명이면 채널 unknown(옛 정상값 대체 금지) |
| 경제성 근거 | admission 결과 `snapshot_id`·`anchor_at` 필수; outcome 은 `snapshot_id == sizing_snapshot.ref`, `anchor_at == row[anchor]`, 모든 게이트 `as_of ≤ anchor` 검사(`ADMISSION_SNAPSHOT_MISMATCH`/`ADMISSION_ANCHOR_MISMATCH`/`ADMISSION_GATE_FUTURE`) |
| 비용 보고 | 시도별 대사액 우선 → 추정액 → unknown; 그날 모든 행의 고유 attempt 1회 집계(같은 판단 여러 실행 포함, `pair_runs`); blocked 마커 행은 excluded 분모; `build_daily_report(..., offer_records=)` 로 저널 레코드에서 모집단/제외 유도, CLI `--daily --offers` |
