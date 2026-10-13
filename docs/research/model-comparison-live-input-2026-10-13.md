# 모델 비교 — 판단 전 입력 봉투·평가 기준시각 v2·비교 작업자 (2026-10-13, W2)

인계 정본: [live comparison handoff](../superpowers/plans/2026-10-13-live-comparison-claude-handoff.md) Task2·Task3,
인터페이스: [구현 분담](../superpowers/plans/2026-10-13-live-comparison-implementation-plan.md) I2·I3·I4.
상태: **로컬 구현·오프라인 검증만.** 기본 OFF, 운영 미배포, 유료 호출 0, 실험 manifest 활성화 없음.
구현 ≠ 검증 ≠ 병합 ≠ 배포 ≠ 수익성 입증.

## 처리 흐름

```
engine.on_signal ── capture_policy_sizing_if_enabled (기존, 기록기 활성 시만 스냅샷)
  └─ cross_validator.llm_second_check
       ├─ 공통 입력 build_entry_context + 구성 시각(collected_at)       ← 기존
       ├─ decision_id = new_id("dec")  ← 여기서 한 번(ContextVar, 호출별)
       ├─ [_comparison_handoff 가 있으면] 봉투 생성 → handoff(envelope) (put_nowait, 예외 삼킴)
       ├─ await adversarial.validate / 단일 폴백                         ← 기존, 불변
       └─ _finish_decision → decision_record(decision_id=같은 ID) → 기록기
                                                     │
작업자 스레드(자체 루프) ◀──── queue.Queue(max 8) ◀──┘
  offer 검사: 봉투 무결성·HMAC → 운영 조건/manifest 일치 → 입력 나이 → 중복 → 큐 여유
  dispatch 직전 나이 재검사(expired) → PairSpec(v2, live 증명) → PairedComparisonRunner.run
  → ComparisonCallGuard(공유 월 원장) → comparison_pairs 결과 저장
```

## Task2 — 입력·ID·출처

**PLAN.** 훅 지점은 `llm_second_check` 에서 `prompt` 가 완성된 직후(공통 입력·위험 안내·폴백 접미가 확정)이고
`self._adversarial.validate(...)` await 전이다. 일일 한도·강세장·점수 미달·LLM 매니저 없음으로 실제 호출 전에
끝나는 판단(`stage="unavailable"` 또는 즉시 통과)은 봉투를 만들지 않으며, 기록이 남는 경우 ID 는 기록 시점에 새로
만든다 — "유료 시도에 도달한 판단(봉투 있음)"과 구분된다. 적대검증기 초기화 실패로 Bull/Bear 패널이 없는 경로도
비교 대상이 아니므로 봉투를 만들지 않는다.

**DO.**
- `decision_record(..., decision_id=None)` 선택 인자: 주면 그 값, 생략하면 기존처럼 새 ID(구형 호출 호환).
- 판단 ID·공통 입력 구성 시각·전달 상태를 ContextVar 로 옮겼다. 이전에는 구성 시각이 인스턴스 속성이라 동시
  판단 두 건이 서로의 시각을 덮어쓸 수 있었다.
- 기록 실패 시 `last_llm_decision.decision_id` 는 이제 None 이 아니라 호출 전 배정 ID 다(봉투·pair 조인용).
  `record_status="error"` 는 그대로이며 기록이 존재한다는 뜻이 아니다.
- 전달을 시도한 경우에만 `last_llm_decision.comparison_handoff`(accepted/rejected_*/handoff_error) 키가 생긴다.
  기본 OFF 에서는 신호 메타가 바이트 단위로 이전과 같다. 판단 결과·fail-open·soft-reject 경로는 불변이다.

### 봉투 `comparison-input-envelope-v1` (`src/analytics/model_comparison_input.py`)

| 필드 | 의미 |
|---|---|
| `decision_id, signal_id, symbol, strategy, score, market_regime` | 판단 식별·조인 키 |
| `common_context`, `common_context_sha256` | 실제로 보낸 공통 사실 원문과 해시 |
| `role_controls.bull/bear` | 운영이 실제 쓰는 `{model, max_output_tokens, reasoning_effort, temperature, seed, timeout_seconds}` |
| `role_controls.fallback_chain` | STRATEGY_ANALYSIS 경로 primary→fallback, `max_tokens=100` |
| `role_controls.fallback_prompt_suffix`, `fallback_system` | 단일 폴백 프롬프트의 공통 입력 뒤 접미, system(없음 `""`) |
| `role_controls.empty_retries`, `strict_on_disagreement` | **I2 추가 키** — 빈 응답 재시도 1, 불일치 정책(운영 False) |
| `neutral_sizing` | 판단 전 수량 스냅샷 또는 None(기록기 비활성) |
| `producer` | `run_id`(프로세스), `pid`, `code_version`(applied_sha), `source_sha256`, `config_sha256`(manifest), `experiment_id` |
| `input_available_at` | 공통 입력 구성 시각(KST aware) = 판단 기록 `inputs.collected_at` |
| `handed_off_at`, `received_at` | 생산자 전달·작업자 수락 시각(해시 제외) |
| `envelope_sha256` | `handed_off_at/received_at/attestation` 를 뺀 본문의 정렬 JSON sha256 |
| `attestation` | `{"kind":"in_process_nonce","nonce_sha256": HMAC-SHA256(프로세스 난수, envelope_sha256)}` |

역할 조건 출처: `adversarial_validator.MAX_TOKENS(400)/VERDICT_SEED/LLM_TIMEOUT(12)/STRICT_ON_DISAGREEMENT`,
Bull `reasoning_effort="minimal"`·Bear `temperature=0.0`, 모델은 `LLMManager._get_model(provider, "light"|"heavy")`
런타임 값. Bull 과 폴백은 온도를 넘기지 않아 원시 클라이언트 기본 0.3 이다(gpt-5 계열은 본문에서 생략). 모델을 못
읽으면 None 으로 두며, 작업자는 manifest 와 다르므로 `rejected_control_mismatch` 로 거부한다.

**출처의 한계.** `nonce_sha256` 은 I2 이름을 유지했지만 값은 고정 난수의 해시가 아니라 봉투 해시에 대한 HMAC 이다.
고정 해시는 한 번 노출되면 다른 봉투에 복사할 수 있기 때문이다. HMAC 은 "이 프로세스가 이 본문을 만들었다"까지만
보인다. 생산 코드의 정당성·원천 자료 진위는 인증하지 않으며, 파일로 전달·재적재된 봉투(다른 프로세스)는 검증되지
않는다(`caller_supplied_not_attested`).

### 평가 기준시각 v2 (I3, `model_comparison_runner.VERSION='model-comparison-pair-v2'`)

결정: 연구 문서의 **공통 입력 시각 + 900초**를 live 계약으로 채택한다. 기존 코드는 작업자 `requested_at`+900 이었다.

- `PairSpec` 추가: `evaluation_anchor`(기본 `requested_at`), `enqueued_at`, `live_provenance`(기본
  `caller_supplied_not_attested`), `attestation`. `paired_live_shadow` 는 `evaluation_anchor='input_available_at'`,
  `live_provenance='in_process_attested'`, `enqueued_at`, 그리고 같은 프로세스 HMAC(`attest_pair`) 검증을 모두 요구한다.
  라벨만 붙인 spec 은 `live_provenance_unattested` 로 거부된다. attestation 은 실행별 증거라 pair manifest·행
  `policy` 에서 제외한다(pair 동일성은 그대로).
- 행 추가: `input_available_at`, `enqueued_at`, `requested_at`(dispatch, 기존 의미), `evaluation_anchor`,
  `evaluation_at=row[anchor]+horizon`, `live_provenance`(spec 값). arm 별 `queue_delay_ms=requested_at−enqueued_at`
  (없으면 None), `provider_latency_ms`(그 arm 의 시도 latency 합 — 병렬 Bull/Bear 합이라 벽시계 지연이 아니다,
  하나라도 None 이면 None), `decision_available_at`(기존).
- `report_pairs`: v1 행은 거부하지 않고 `evaluation_anchor='requested_at'` 로 그대로 해석하며
  `legacy_v1_requested_at_anchor` 사유를 붙인다. live 라벨이지만 증명·입력 기준이 아닌 행은
  `live_provenance_not_attested`. 보고서 `schema_version` 은 W3 연동을 위해 `model-comparison-report-v3` 유지.

## Task3 — 작업자 (I4, `src/analytics/model_comparison_worker.py`)

**PLAN.** 전용 데몬 스레드 + `asyncio.run` 루프 하나(작업자 1명이 소유). 큐 `max_queue=8`, 입력 나이
`max_input_age_seconds=5.0`(offer 와 dispatch 직전 두 번), `max_concurrent_pairs=1`(다른 값은 생성 거부), 종료
`stop(deadline=20)`. 운영 루프에서는 `offer()` 의 검사와 `put_nowait` 만 일어난다(락은 카운터용, 대기 없음).

카운터: `offered/accepted/rejected_full/rejected_disabled/rejected_stale/rejected_duplicate/rejected_unattested/
rejected_invalid/rejected_control_mismatch/expired/started/finished/incomplete`. **I4 대비 추가:** `rejected_invalid`
(시계 역전·필드 결함·수량 스냅샷 신원 불일치), `rejected_control_mismatch`(허용된 추가). 무결성 실패
(`envelope_hash_mismatch/context_hash_mismatch/unattested`)는 `rejected_unattested` 다.
`accepted` 는 큐 접수일 뿐이며 `finished` 만 pair 결과 저장 완료다.
**P1 후속 추가:** `rejected_daily_pair_cap`·`rejected_input_too_large`(아래), `blocked`(러너가 쌍 입장 전 표식으로
막음), `journal_dropped`(저널 큐 포화)·`journal_failed`(저널 쓰기 실패).

offer 순서: 비활성 → 봉투 검증 → 시계 → 조건/생산자 일치 → 입력 나이 → 중복 → **입력 크기**(`common_context` UTF-8
바이트 > 정책 `max_common_context_bytes`) → **하루 쌍 상한**(그날 센 pair 수 ≥ 정책 `pairs_per_day`) → 큐. 그날 센
pair 는 `start()` 때 원장 `comparison_pairs.admission_day=오늘` 에서 채우고(재시작 뒤에도 유지) 수락 때 더한다 — 운영
루프에서 파일·DB 를 읽지 않는다. 이미 센 pair 의 재전달(재시작 뒤 같은 판단)은 러너가 저장 결과를 돌려줘 지출이 없으므로
상한에 다시 세지 않는다. 러너에 닿지 못한 수락분(dispatch 만료·종료 미시작)은 그날 수에서 빼고, 러너가 실패하면
작업자 스레드가 원장 `admission_day` 를 다시 읽어 입장하지 않은 pair 를 뺀다(확인 불가면 센 채 둔다). 최종 판정은 러너
claim(같은 쓰기 잠금 안의 원장 카운트)이다.

manifest(작업자가 읽는 키, W3 manifest 와 통합 시 정렬 필요): `experiment_id`(guard 정책과 같아야 함), `enabled`
(true 가 아니면 생성 거부), `fixed_at`, `horizon_seconds`, `strict_on_disagreement`, `empty_retries`, `current`,
`candidate`, `bear`, `fallback_chain`(RoleControls 필드), `fallback_chain_timeout_seconds`(체인 전체 시한, 필수),
런처용 `execution_policy`(`ComparisonExecutionPolicy` 인자, `experiment_id` 제외 — `pairs_per_day`·`max_common_context_bytes`
포함). 봉투의 운영 Bull 조건이 `current` 와, Bear·폴백·폴백 체인 시한·재시도·불일치 정책이 manifest 와 같고 `producer` 의 실험 ID·manifest 해시·소스 지문·run_id·pid 가 이 프로세스 값과 같아야 수락한다.

생명주기:
- 재시작 시 큐는 사라진다. SQLite `comparison_pairs` 의 result NULL 행은 자동 재실행하지 않는다(러너가
  `comparison_pair_incomplete_no_automatic_retry` 로 거부, 조회는 Task1 `inspect_pair`).
- 같은 decision_id: 같은 작업자에서는 `rejected_duplicate`, 재시작 뒤(새 작업자·같은 원장)에는 러너가 저장된 행을 돌려줘
  공급자 호출·예약·과금 0.
- `stop(deadline)`: 수락 차단 → 시한 내 drain → 시한 초과 시 진행 작업 취소·남은 큐는 `incomplete`
  (`shutdown_not_started`, `admitted=False`). 실행 중 취소는 영속 입장 여부를 조회하며 조회 실패는 unknown으로 남긴다. 입장 전 종료분은 실행 표본에 넣지 않는다. 공급자가 이미 받았을 수 있는 시도는 원장에 미해결로 남고 재전송하지 않는다.
- `PairResultPersistenceError` 의 원래 행은 incomplete 저널(아래)에 영속 보존하고 `incomplete_results`(메모리,
  최근 64)에도 둔다. 복구는 `restore_pair_result(..., evidence_path=그 저널)` 로만 한다.
- 스레드 종료 시 원시 클라이언트의 aiohttp 세션을 작업자 루프에서 닫는다. 저널 봉인은 생산자 종료 뒤에 한다. 동기 저장이 지연되어 `stop()` 제한 시간을 넘으면 아직 살아 있는 생산자의 저널을 미리 닫지 않고 `journal_drained=False`를 반환한다. 이후 생산자가 종료되면 늦은 복구 행과 전이까지 배출하고 봉인한다. `journal_drained`는 단독 저장 성공 증거가 아니며 실패/드롭 수와 reader의 완전성을 함께 확인한다. 강제 프로세스 종료·저장장치 장애까지 영속 기록을 보장하는 것은 아니다.

저널의 `run_id`는 작업자 인스턴스마다 새로 만든다. 같은 프로세스의 새 작업자도 별도 번호 스트림을 사용하며, 입력 봉투의 프로세스 attestation ID는 바꾸지 않는다. 종료 표식의 `journal_dropped_total`은 같은 run의 드롭 마커와 최댓값으로 합친다. 드롭 마커가 offers에만 남아도 incomplete 파일의 누락을 보존한다. 누계가 명시됐으나 잘못된 값이면 종료 증거로 인정하지 않는다. 중복 시퀀스는 결손으로 처리해 이전 종료 표식으로 새 실행을 완료 처리하지 않는다.

저널(append-only JSONL, `journal_dir=` 주입, 기본 `Path.home()/.cache/ai_trader/comparison_worker`, KST 월 파일):
- `offers-YYYY-MM.jsonl` — 종료 전 offer 마다 `{schema_version:"comparison-offer-v1", kind:"offer", recorded_at, decision_id,
  status, reason, received_at, envelope}`(봉투 전체·attestation 포함, 거부 포함), 상태 전이마다 `{schema_version, kind:
  "transition", decision_id, status ∈ expired|started|finished|incomplete|blocked, reason, at, pair_id}`.
- `incomplete-YYYY-MM.jsonl` — `{schema_version, kind:"incomplete_result", recorded_at, decision_id, pair_id, row_sha256, row}`.
- 운영 루프의 `offer()` 는 유한 큐(`journal_queue=1024`)에 `put_nowait` 만 하고 저널 스레드가 쓴다(fsync). 포화는
  `journal_dropped`, 쓰기 실패는 `journal_failed` 로 세며 판단·offer 결과를 바꾸지 않는다. 작업자 스레드의 전이·
  incomplete 기록은 저널 큐가 차면 각각 최대 1초·5초 기다린다(운영 루프 아님).
- 읽기: `read_offer_journal(path, *, max_bytes)`·`read_incomplete_journal(path, *, max_bytes)` →
  `{path, missing, records, skipped_lines}`. 손상·형식 불일치 줄(incomplete 는 `row_sha256` 불일치 포함)은 건너뛰고 수를
  센다. 파일이 `max_bytes` 보다 크면 일부만 읽지 않고 `ValueError('journal_too_large')`. 일별 보고(W-B)의 모집단·제외 입력이다.

### 런처 배선 (기본 OFF)

`scripts/run_trader.py`: KR 초기화에서 `install_from_env(engine_risk_manager._cross_validator)`.
`COMPARISON_WORKER=1` **그리고** `COMPARISON_EXPERIMENT_MANIFEST=<경로>` 가 있고 manifest `enabled=true` 일 때만
작업자를 만들고(운영 LLMManager 가 이미 연 공유 월 원장 사용, 새 원장·기준선 생성 없음, 원시 클라이언트는
`LLMConfig.from_env()`), `cross_validator._comparison_handoff=worker.offer` 를 연결한다. 그 외는 아무 변화 없음.
설치 실패는 경고 후 None. 종료: 작업자는 공유 원장을 쓰므로 `close_llm_manager()`(원장 close 포함) **전에**
`asyncio.to_thread(worker.stop, 20)` 하고 결과를 로그에 남긴다 — 기록기 종료보다 앞이다.
서비스·타이머 설치 없음.

검증 진입점: `venv/bin/python scripts/dev/comparison_worker_dryrun.py` — 가짜 공급자·임시 원장·합성 봉투로
offer→실행→stop 을 한 번 돌려 health JSON 을 출력한다(네트워크 0, rc 0 = 수락·완료·drain).

## SEE — 검증

- `tests/test_model_comparison_input.py`(11): 봉투 증명·해시 범위, 운영 조건 복사, 섞인 공통 입력/변조/다른
  프로세스 난수/증명 복사 거부, 시계 역전, **완료된 월 JSONL 기록으로 만든 spec 의 live 거부(라벨만 붙여도 거부)**,
  공급자 await 전 전달·ID 전파, 전달 예외 시 판단·메타 불변, 기본 OFF·unavailable 무봉투, 기록기 비활성 시 ID 1회·
  수량 None, 동시 판단 2건 격리, `decision_record(decision_id=)` 호환.
- `tests/test_model_comparison_worker.py`(11): 실행·중복·재시작 무과금, 봇 종료 시 작업자 stop 이 LLM 매니저(원장 close)보다 먼저, 미시작/종료 거부, 무결성·무효·조건 불일치·
  낡은 입력 거부, 큐 포화·dispatch 만료(offer 비대기), 종료 시한 초과 취소·미재전송, 저장 실패 원래 행 보존,
  manifest 검사, 런처 기본 OFF·비활성 manifest 무클라이언트, cross_validator→작업자 E2E(판단·메타 불변, ID 동일),
  dry-run 진입점(격리 가드 아래). `run_trader.shutdown` 은 `__init__` 을 거치지 않은 객체도 다루도록 `getattr` 로 작업자를 읽는다.
- P1 후속(같은 날): 작업자 시험 +8 — offer 단계 하루 쌍 상한·입력 크기 거부와 재시작 뒤 상한 유지(같은 판단 재전달은
  지출 0), 만료 수락분·claim 전 실패분 미소진(원장 재확인), 폴백 체인 시한 불일치 거부, offer·전이 저널(호출 스레드 파일 open 0)·incomplete 행 저널→
  복구→캐시(공급자 호출 0), 저널 포화·쓰기 실패가 offer 결과 불변, 읽기 함수의 손상 줄 집계·부분 읽기 거부.
  러너 시험 +4(쌍 상한·표식·다음날 미재실행, 입력 바이트 상한, 체인 시한에서 두 번째 폴백 호출 0, 시한 필수).
- `tests/test_model_comparison_runner.py`: v2 시각 분리·입력 기준 평가, v1 행 원래 의미 보고. 기존 live 시험은
  `live()` 헬퍼로 증명된 spec 을 쓰도록 갱신(의미 보존). 기존 `frozen_capture` 추가 — 스냅샷 `captured_at` 이 실제
  시각이라 고정 `NOW`(2026-10-13 12:00 UTC) 이후 두 시험이 기준선에서도 실패하던 시계 의존 결함(제품 검사는 불변).

## 한계·미완료

- 출처 증명은 같은 프로세스 난수 범위다. 파일 전달·다른 프로세스는 비인증이며 지원하지 않는다.
- 폴백 시한: P1 후속으로 러너가 `PairSpec.fallback_chain_timeout_seconds`(운영 `SINGLE_FALLBACK_TIMEOUT` 복사,
  `role_controls.fallback_chain_timeout_seconds`)를 체인 전체에 적용한다. 시도별 `timeout_seconds` 도 그대로 남는다.
  manifest `known_control_differences` 의 "시도별 10초" 문장은 통합 때 현재 구현(체인 전체 10초)대로 고쳤다.
- 하루 쌍 상한의 작업자 사전 검사는 이 프로세스가 유일한 입장자라는 가정의 메모리 카운트다. 다른 프로세스가 같은
  원장에 입장시키면 러너 claim 이 최종 차단한다(공급자 호출 전).
- 공통 입력 원천 관측 시각(`source_observed_at`)은 여전히 None — `input_available_at` 은 구성 시각이지 원천 시각이 아니다.
- 예산 정책 v2(`experiment_usd_ceiling`, 유효 기간, `required_policy_mode`)와 `known_reconciled` 상태는 W1 소관이다.
  러너는 guard lookup 의 `state` 를 문자열로 통과시키며, 작업자 시험의 정책 setup 은 W1 통합 시 v2 필드로 바꿔야 한다.
- 진입 조건·장 상태·경제성 판정(W3)은 없다. pair 행은 계속 `economic_evaluable=False`, `actual_execution=False`.
