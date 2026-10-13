# R1 실험 명세·진입 조건 재생·장 상태 분류·일별 보고 (2026-10-13, W3)

목표: Bull 추론 수준 하나(minimal→low)를 바꿨을 때 **응답 뒤 첫 호가·정책 수량·비용 후 조건부 가격 손익**이 달라지는지 측정할 준비. 이번 작업은 순수 함수·OFF manifest·읽기 전용 보고만 추가한다. 실험은 **미등록·미예약·enabled=false**, 유료 호출·KIS 호출·주문·발송 0.

인계 정본 [handoff](../superpowers/plans/2026-10-13-live-comparison-claude-handoff.md) Task4·Task5, 고정 인터페이스 [I5](../superpowers/plans/2026-10-13-live-comparison-implementation-plan.md).

## 산출물

| 파일 | 역할 |
|---|---|
| `src/analytics/model_comparison_admission.py` | `replay_admission`(LLM 이후 게이트 재생), `classify_market_state`(raw 장 상태 → normal_continuous/not_normal/unknown), 게이트·코드 의미 상수표와 출처 |
| `src/analytics/model_comparison_outcome.py` | `compare_policy_outcomes` v2: `evaluation_anchor ∈ {requested_at, input_available_at}`, arm 별 admission·market_state 게이트, `assumptions` |
| `src/analytics/model_comparison_daily_report.py` | `worst_case_reservation`(manifest 토폴로지 최악 예약), `select_daily_sample`, `build_daily_report`(분모·지연·수량·비용·조건부 경제성·텔레그램 미리보기 문자열) |
| `scripts/report_model_comparison.py` | `--daily YYYY-MM-DD [--envelopes] [--exclusions]` 읽기 전용 |
| `config/experiments/R1-bull-reasoning-20261013.json` | OFF 실험 manifest |

## Task4 PLAN — LLM 이후 실제 진입 게이트 (소스 `c4e48e7`)

`on_signal` 의 `llm_second_check`(engine.py:2071) 뒤 순서다. **재생 가능**은 판단 전 중립 스냅샷(`capture_policy_sizing`, as-of=`captured_at`)의 `state/config` 와 `replay_policy_sizing` 수량만으로 같은 비교를 다시 계산할 수 있다는 뜻이다. LLM await 동안 장부가 바뀔 수 있어 "판단 전 장부 기준 통과"이며 주문 시점 통과의 증명이 아니다.

| 게이트 | 근거 | 입력 / as-of | 처리 |
|---|---|---|---|
| 가용현금 사전 차단 G5_cash | engine.py:2130-2146 | available_cash−reserved−core_reserve / 스냅샷 | 재생 |
| 전략 예산 사전 차단 G5_budget | engine.py:2147-2166 | strategy_current+pending, equity×allocation / 스냅샷 | 재생 |
| 팩터 버킷 G5_factor | engine.py:2168-2179 | factor_budgets 설정·노출 — 스냅샷에 없음 | unknown |
| 주문 실패 쿨다운 | engine.py:2181-2189 | 런타임 dict | unknown |
| 사이징: 현금 | engine.py:3096-3106 `no_available_cash` | trace.stop | 재생 |
| 사이징: 전략 한도 | engine.py:3166-3184 `strategy_budget_exhausted` | trace.stop | 재생 |
| 사이징: 최소 금액 | engine.py:3271-3287 `below_min_position_value` | trace.stop | 재생 |
| 사이징: 위험 0.7% 클램프 | engine.py:3311-3333 `risk_quantity_cap`/`risk_cap_below_min_position_value` | trace | 재생(클램프=passed) |
| 수량 0 G3_size | engine.py:2211,2226-2238 | replay.quantity | 재생 (`size_zero:<stop>`) |
| 취소 최종 체결 미확인 | engine.py:2222-2224 | 런타임 | unknown |
| 섹터 조회·한도 | engine.py:2286-2293, 858-883 | 외부 조회·pending 섹터 | unknown |
| 검증자(risk/manager.py) 현금 검증·거시 진입·당일 손절/청산 재진입·동기화 건강성·스마트 일일 손실·신규 매수 수·보유 수·예비현금·자체 크기/현금·손실청산 쿨다운 | manager.py:287-523 | 검증자 내부 상태 | unknown |
| 엔진 일일 손실 하드캡 | engine.py:822-829 | effective_daily_pnl/equity, daily_max_loss_pct×2.5 / 스냅샷 | 재생 (engine.config.risk = RiskManager.config 배선 가정, run_trader.py:848-849) |
| 엔진 일일 거래 횟수 | engine.py:831-849 | 체결+pending BUY | unknown |
| 엔진 포지션 크기 | engine.py:885-889 | price×q vs equity×max_position_pct | 재생 |
| 엔진 현금 | engine.py:891-896 | price×q×1.001 vs 가용−(예약+코어예약) | 재생 |
| pending 등록 경쟁 | engine.py:2353-2356 | 런타임 | unknown |
| 매수 중지 KILL_SWITCH(_KR) | 브로커 계층 | 파일 상태(제출 시점) | unknown |
| 접수 불명 BUY 보류 | engine.py:2011-2015 + 브로커 | 브로커 장부 | unknown |
| 증권사 매수가능·한도 | KIS 응답 | 외부 | unknown |

`admissible` = 실패가 하나라도 있으면 False, 아니면 unknown 이 하나라도 있으면 None, 모두 passed 일 때만 True. **외부 게이트가 항상 unknown 이므로 현재 코드에서 True 는 나오지 않는다** — 근거 생산자가 생기기 전까지 경제성 계산을 막는 의도된 결과다. 사이징이 앞 단계에서 멈추면 뒤 단계는 `not_reached:<stop>` unknown(보수적; `quantity_below_one` 등 순서 밖 사유도 전부 unknown).

## Task4 PLAN — 장 상태 코드 의미와 출처

출처는 공식 1차 문서 KIS 개발자 포털 공개 상세 API(인증 불필요)다. 저장소 `kis_market_state._TR_SPECS` 의 `source_sha256` 와 같은 바이트(10/12 조회 원문, Git 밖 `~/.local/share/qwq-research/public-kis-status-20261012/`)를 이번에 다시 해시해 일치를 확인했고, 10/13 WebFetch 재조회로 설명 문구와 필드 수정일이 같음을 확인했다(WebFetch 는 변환 본문이라 해시 비교 대상이 아니다).

| TR | URL | sha256 (10/12 원문) | 필드 수정일 |
|---|---|---|---|
| H0STCNT0 | https://apiportal.koreainvestment.com/api/apis/public/detail?accessUrl=%2Ftryitout%2FH0STCNT0 | `833b105418e98807d3e24753f97ed008e0f5f6972a0937a1a0f40d252543624d` | 2026-09-11T16:31:27+09:00 |
| H0STMKO0 | https://apiportal.koreainvestment.com/api/apis/public/detail?accessUrl=%2Ftryitout%2FH0STMKO0 | `4c76c54995ff112312ecf9e98f4f194d6edfe2f0749300b8639c10ff0d40c3f5` | 2025-04-30T13:11:13+09:00 |

| 필드 | 확정한 의미(원문) | 분류 사용 |
|---|---|---|
| CNT `NEW_MKOP_CLS_CODE` | 첫 자리 1 장개시전·2 장중·3 장종료후·4 시간외단일가·7 일반Buy-in·8 당일Buy-in / 둘째 0 보통·1 종가·2 대량·3 바스켓·7 정리매매·8 Buy-in | `20` 정상, 문서화된 다른 조합 not_normal, 그 외 unknown |
| CNT `TRHT_YN` | Y 정지 / N 정상거래 | N 정상, Y not_normal |
| CNT `HOUR_CLS_CODE` | 0 장중·A 장후예상·B 장전예상·C 9시 이후 예상가·VI 발동·D 시간외 단일가 예상 | 0 정상, A~D not_normal |
| CNT `MARKET_CLS_CODE` | 1 프리·2 정규·3 애프터·5 종가 | 2 정상, 1/3/5 not_normal |
| MKO `MKOP_CLS_CODE` | 110 장전동시호가·112 장개시·121 장후동시호가·129 장마감·130~159 시간외·164 시장임시정지·174/182/184 서킷 발동/동시마감/개시·175/185 해제·387~398 사이드카·F**/P** 예고·`???` | 112 만 양립, 110/121/129/130~159/164/174/182/184 not_normal, 나머지 unknown |
| MKO `VI_CLS_CODE` | Y VI 적용 / N 미적용 | N 필요, Y not_normal |
| MKO `ISCD_STAT_CLS_CODE` | 51 관리·52~54 시장경고·55 신용가능·57 증거금100·58 거래정지·59 단기과열·00 그 외 | 00/55/57 중립, 58 not_normal, 51~54·59 unknown |

**미확정(unknown 유지)**: MKO `TRHT_YN`(설명 공란 — Y 만 보수적으로 차단, N 은 정상 근거로 쓰지 않음), MKO `EXCH_CLS_CODE`(공란), CNT `MRKT_TRTM_CLS_CODE`(공란), MKO `ANTC/MRKT_TRTM/DIVI`(동시호가 사건용, 분류 미사용), `???` 코드, F/P 예고·사이드카·서킷 해제의 연속매매 여부, ISCD 51~54·59 의 연속매매 영향.

`normal_continuous` 조건: 같은 연결·세대의 최신 CNT 기록이 `now` 이전·`max_age` 이내이고 20/N/0/2, **그리고** 같은 연결·세대의 최신 MKO 기록이 112·VI N·종목상태 중립. `now` 이후 기록은 무시한다. 결측 이벤트(특히 사건식 MKO 의 침묵)·빈 값·오래된 상태·다른 연결/재연결 전 세대·다른 채널·미검증 레코드·미지 코드·여러 종목 혼합은 unknown, CNT 반대 근거는 not_normal, CNT 정상인데 MKO 반대이면 `channel_conflict` unknown. **채널별 최신(now 이전) 기록을 먼저 고른다** — 그 최신 기록이 미자격(`invalid_values`/`unsupported_layout`/스키마·해시 불일치)·다른 연결/세대이거나 채널에 시각 불명 기록이 있으면 그 채널은 `latest_record_unqualified:<tr>` unknown 이고, 더 이른 정상 기록으로 대체하지 않는다(정상 뒤 무효·세대 전환 = 지금 상태 불명). 무효 기록이 정상보다 이르면 영향 없음. 구독 직후 MKO 첫 상태가 오는지는 10/14 파일럿 전 미확인이므로 **실제로는 unknown 이 기본값일 수 있다.**

## Task4 DO — 조건부 결과 계산 (I5)

`compare_policy_outcomes`(schema `model-policy-outcome-v2`):

- anchor = `policy.evaluation_anchor`, 기준시각 = `row[anchor]`. 행에 `evaluation_anchor` 가 있으면 일치해야 한다. `input_available_at` 은 `live_provenance='in_process_attested'` 필수, `requested_at` 이 있으면 입력 시각 이후여야 한다. `evaluation_at = anchor + horizon`, 스냅샷·정책 고정·FX 시각 ≤ anchor, arm 완료 ≥ anchor.
- `requested_at` 은 legacy(작업자 dispatch 시각) — 계산은 유지하되 `assumptions` 에 `legacy_requested_at_anchor_is_worker_dispatch_time`.
- arm 별: `row['admission'][arm]` 이 admission schema·`replay_id==sizing_replay_ref`·수량·판정 일치. 수량>0 arm 은 `admissible is True` 이고 **모든 게이트 이름이 passed**(일괄 True 표시는 대체 불가), `row['market_state'][arm]` 이 normal_continuous 이고 `evaluated_at == entry_quote.received_at`. 수량>0 arm 이 있으면 `market_state['exit']` 도 exit 호가 시각에 normal. 수량 0 arm 은 `sizing_quantity` failed 와 일치하면 현금(0)으로 계산한다.
- 근거 출처 대조(Codex #6): 모든 arm 의 admission `snapshot_id == sizing_snapshot.ref`(아니면 `ADMISSION_SNAPSHOT_MISMATCH`), `anchor_at` 이 `row[evaluation_anchor]` 와 같은 순간(오프셋 표기 차이 허용, 아니면 `ADMISSION_ANCHOR_MISMATCH`), 판정에 쓴 게이트(수량>0 은 전 게이트, 수량 0 은 `sizing_quantity`)의 `as_of` 가 tz-aware 이고 anchor 이하(미래 `ADMISSION_GATE_FUTURE:<gate>`, 누락·naive·형식 오류 `ADMISSION_GATE_TIME_INVALID:<gate>`). `replay_admission` 결과는 `snapshot_id`·`anchor_at` 을 싣는다.
- 하나라도 어긋나면 `net_delta_krw=None`, `arm_reasons={arm: [...]}`, `reasons=['arm:REASON', ...]`.
- 기존 비용·FX·슬리피지·호가 잔량·`actual_execution=False` 검사는 그대로. `assumptions` 에 "표시 잔량은 체결/VWAP/슬리피지 인증 아님", "슬리피지·수수료는 가정", "판단 전 스냅샷 기준 재생", "raw 코드 판독은 거래소 인증 아님".

## Task5 — R1 manifest (`enabled:false`)

| 항목 | 값 | 근거 |
|---|---|---|
| Bull current / candidate | gpt-5-mini, reasoning `minimal` / `low`, max 400, seed 20260803, timeout 12 | adversarial_validator `_ask_bull`; `_OPENAI_EFFORTS['gpt-5-mini']` 에 minimal·low 둘 다 있음 |
| Bear | gemini-3.1-flash-lite-preview, max 400, temperature 0.0, reasoning 없음 | `_ask_bear` (운영 값과 일치) |
| 폴백 | gpt-5.6-sol 100 → gemini-3.1-pro-preview 100, system 없음 | cross_validator.py:910 `STRATEGY_ANALYSIS`, llm.py:688 |
| strict_on_disagreement | false | `STRICT_ON_DISAGREEMENT=False` |
| 쌍/일·동시 | 1·1 | 초기 기술 표본 |
| horizon·anchor | 900초·input_available_at | I3 |
| 입력 나이 | ≤5초 | |
| 빈 재시도 | 1 | `retry_on_empty=1` |
| 공통 입력 상한 | 16,384 바이트 | 비용 상한 전제(초과 봉투는 선정 제외) |
| 유효기간 | 2026-10-15 ~ 2026-11-11 KST(양끝 포함, 20거래일 후보) | 휴장일은 활성화 때 재확인 |

알려진 차이(2차 수정으로 해소): 운영 폴백 10초는 두 공급자 합산 시한이며, runner 도 `fallback_chain_timeout_seconds`(10.0)로 체인 전체에 같은 시한을 적용한다. 각 폴백 시도의 `timeout_seconds` 는 그 안의 상한이다(시도 수·비용 상한 불변).

### 최악 시도·USD 상한

시도/쌍 = (1+1) 공통 Bear + 2 arm × [(1+1) Bull + 2 폴백] = **10**. 시도별 예약 = `llm_budget.reserve` 식 — 입력 2×(prompt 바이트+system 바이트+256) 토큰, 출력 상한(Gemini 2배), `rate_quote` 단가, `_units` 올림. prompt = 16,384 + 역할 접미.

| 시도 | 모델 | 입력 토큰 | 출력 예약 | USD |
|---|---|---|---|---|
| bear ×2 | gemini-3.1-flash-lite-preview | 34,570 | 800 | 0.0098425 |
| bull ×2 arm ×2 | gpt-5-mini | 33,998 | 400 | 0.0092995 |
| 폴백1 ×2 arm | gpt-5.6-sol | 33,698 | 100 | 0.136792 |
| 폴백2 ×2 arm | gemini-3.1-pro-preview | 33,698 | 200 | 0.069796 |

- `daily_attempt_cap` = 10 × 1쌍 = **10**
- `reservation_usd_ceiling_per_pair` = **$0.4700590** (폴백이 88%)
- `experiment_usd_ceiling` = 20거래일 × 1쌍 × $0.4700590 = **$9.4011800**

manifest 수치는 `worst_case_reservation(manifest)` 와 같음을 시험이 확인한다. 과거 $99.70 잔액·과거 비용표 쌍 수는 쓰지 않았다. 예약은 실측 비용이 아니며 정산 뒤 실제 사용액은 더 작을 것으로 예상되나 그 값을 상한으로 쓰지 않는다.

회계: 같은 월 USD100 원장(별도 금전 원장 없음). 실험 상한은 유효기간 전체에 고정되며 **KST 월초 운영 리셋이나 실험 ID 변경으로 보충되지 않는다**(I1: 새 ID 정책 저장 거부). 80% 절약·95% 필수 보호·100% 중단은 운영 정책 그대로이고, 비교 예약은 이번 예약 포함 80% 초과 시 거부(I1).

### 선정·제외·중단

- **선정**: 그날(KST) 실제 `llm_second_check` 유료 호출에 도달해 봉투가 만들어진 판단 중 `attestation.kind=in_process_nonce`, 입력 나이 0~5초, 공통 입력 ≤ 상한인 첫 1건; 순서 (input_available_at, decision_id). 응답·호가·손익은 보지 않는다.
- **제외**(분모 유지, 경제성만 null): 비용 unknown·타임아웃·모델 불일치·입력 오래됨·장 상태 unknown/not_normal·진입 조건 unknown/failed·호가 결측.
- **중단**: `comparison_daily_attempt_cap`(당일), `comparison_policy_mode_not_normal`, `comparison_economy_threshold`, `comparison_experiment_ceiling`·`comparison_experiment_expired`·`comparison_policy_mismatch`(영구), `comparison_policy_disabled`·`essential_budget_reserved`·`monthly_limit_exceeded`.
- 같은 표본으로 임계값·선정 규칙·지평을 조정하지 않는다. 기술 결함 수정과 가설/버전 변경을 구분하고 가설 변경은 새 실험 ID 로만 한다.
- 소스 지문: base `c4e48e7` 의 `policy_source_hash`·관련 파일 sha256 을 기록했고, W1/W2 가 수정하는 파일은 `pending_at_activation` 자리만 둔다(통합 head 에서 채움, 운영 SHA 와 다르면 활성화 금지).

## Task5 — 일별 보고

`build_daily_report(pair_rows, *, day_kst, offered_envelopes=(), exclusions=(), reconciled_lookup=None, outcomes=None, offer_records=())`:

- `offer_records`(작업자 append-only offer 원장 `comparison-offer-v1`): `kind:offer` 기록의 봉투(+기록 `received_at`, 봉투 decision_id 가 기록과 다르면 무시)를 모집단으로, offer/transition 의 `rejected_*`·`expired`·`incomplete` 상태를 사유(`reason`, 없으면 상태)와 함께 제외로 쓴다. 기존 `offered_envelopes`/`exclusions` 와 합친다. CLI `--daily --offers <jsonl>` 은 `model_comparison_worker.read_offer_journal` 을 지연 import 하며 없으면 rc 2·명확한 오류.
- 차단 마커 행(`model-comparison-pair-blocked-v1`, `reason`)은 그날 제공된 판단이면 그 사유로 excluded 분모에 들어간다(사유 우선순위: 차단 마커 > 명시 제외 > offer 원장).

- 분모 `offered/eligible/sampled/complete/excluded`(excluded = offered − complete), `missing_reasons`(unattested_input·not_received·input_too_old·input_clock_reversal·common_context_too_large·daily_quota·pair_result_missing·partial_result·제외 사유), 중복 전달은 첫 봉투만.
- 행별: 스키마, anchor(v1 은 `requested_at (legacy)`), provenance, 입력/큐/요청/평가 시각, arm 별 판정·시도별 요청/응답 모델·`model_identity`·추론·지연·전송 여부·비용, `queue_delay_ms`·`provider_latency_ms`(v2), 완료−입력 지연, 응답 뒤 첫 호가(`quote_evidence.first_quote_after`, report_pairs 결합 결과가 있을 때), 재생 수량, 시나리오 비용(합산 금지), 조건부 경제성(`compare_policy_outcomes` 결과 또는 null+`outcome_not_supplied`).
- 지출(Codex #7 후): 그날 **모든 실행 행**(같은 decision_id 의 여러 pair 행 포함)의 고유 attempt_id 를 한 번씩. 시도별 비용 = 감사 대사액(`reconciled_lookup` — W1 `guard.lookup` 결과 또는 매핑) > 저장 추정 `api_cost_usd` > 불명. 조회 예외는 대사 여부를 모르므로 불명(`lookup_failed_attempts`). 필드 `original_estimated_usd`(저장 추정 합, 정보용)·`reconciled_usd`·`reconciled_attempts`·`unknown_attempts`·`pair_runs`, 불명이 하나라도 있으면 `total_usd=null`. 판단별 행은 최신 실행(`requested_at`, 동률은 입력 순서)이고 `pair_runs`·`prior_pair_ids` 를 싣는다.
- `stop_conditions_reached`, `telegram_preview`(문자열만, 발송 함수 없음). CLI `--daily` 는 `reconciled_lookup`/`outcomes` 를 받지 않는다(원장 접근 없음).

## SEE — 검증

- RED: outcome 확장 시험 17개가 수정 전 코드에서 실패 → 수정 후 통과. admission 은 as-of 검사와 MKO 필수 조건을 일부러 제거한 변이에서 5개 실패. 일별 보고는 Bear 중복 합산·선정 역순 변이에서 3개 실패, CLI `--daily` 는 구현 전 실패.
- Codex 독립 리뷰 후속(#5~#7, base `1e6feac`): 수정 전 코드에서 장 상태 7건(정상 뒤 무효/세대·연결 전환/시각 불명), 진입 근거 10건(다른 스냅샷·다른 anchor·미래/naive 게이트 시각·수량 0 arm), 일별 보고 2건 의미 재현(추정 $0.0001·대사 $0.00015 → 구 합계 $0.0031, 앞 실행 unknown 소실) + 신규 필드·offer 원장·차단 마커·CLI `--offers` 실패 → 수정 후 관련 5파일 184 passed.
- 범위: 표본 0, 제외만 있는 날, 부분 결과·결과 누락, unknown→대사 비용, 예산 모드 변경 중단, KST 일/월 경계(10-31 23:59:59 / 11-01 00:00:00), 결과를 바꿔도 같은 선정, v1 legacy 표기, 조작된 스냅샷·나중 스냅샷 누설·재생 혼합, VI/정지/세대/채널/충돌/미지 코드.

## 한계

- 일별 보고의 선정(첫 적격 봉투)과 작업자의 실제 수락이 다르면(예: 첫 봉투 큐 거절 뒤 두 번째 실행) 표본은 거절 사유로 제외되고 두 번째 실행은 daily_quota 로 남는다 — 선정 규칙 일치는 W-A 통합에서 확인 필요. `read_incomplete_journal` 은 아직 보고에 연결하지 않았다.

- sizing_only 재생·raw 장 상태·호가 잔량은 실행 가능 수량·체결·실현손익·계좌 수익이 아니다.
- **live readiness 차단**: 외부 게이트 unknown(→ net_delta 항상 null), MKO 첫 상태 수신 미확인, 위 미확정 코드 의미가 하나라도 있으면 차단. W1 admission 훅·W2 봉투/작업자 통합·독립 리뷰 미완료.
- I5 와 다르게 한 점: `replay_admission` 에 키워드 `anchor_at`(스냅샷만 줄 때) 추가, 결과에 `schema_version/anchor_at/snapshot_id/replay_id/verdict/quantity/reasons` 추가; `classify_market_state` 결과에 `schema_version/evaluated_at` 추가; outcome 은 수량>0 arm 이 있으면 `market_state['exit']` 도 요구하고 수량 0 arm 은 admission·장 상태 대신 `sizing_quantity` failed 일치만 요구; 일별 보고에 `max_input_age_seconds/pairs_per_day/max_common_context_bytes` 키워드 추가.
