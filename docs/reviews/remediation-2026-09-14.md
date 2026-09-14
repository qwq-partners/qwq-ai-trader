# 리뷰 후속 수정·재검증 결과 보고 (2026-09-14)

> 계획서: `docs/superpowers/plans/2026-09-13-review-remediation.md` (기준 SHA de111b7). 이 문서는 계획서 §4 "최종 전달 형식" 1~7항에 대응한다.
> 검사 SHA: **main `8f5580a`** (PR #31~#40 머지 후). 운영 서버는 **de111b7(09-13 배포본) 그대로** — 이번 작업에서 배포·재시작·주문·설정·킬스위치 변경은 하지 않았다.

## 0. 한 줄 요약

F1~F8 전부 독립 재현(8/8) 후 수정·머지했다(PR #33~#38, #40). 미래정보를 제거하고 유효 설정·예산 캡을 맞춘 조건에서 nominal/risk 를 다시 계산한 결과, **risk 는 운영 게이트 4조건을 충족하지만 사전 등록 대조군(고정 14% 명목)도 동일하게 통과**하고 parity 미해소 2건이 남아 **승격 보류**다. 검증된 것은 위험 사이징 공식의 고유 효과가 아니라 노출 축소 효과다. canary 는 미시작.

## 1. F1~F8 — 수정 커밋·실패하던 테스트·수정 후 결과·미해결

D(독립 리뷰어)가 de111b7 에서 합성 입력만으로 8건 전부 재현했다(운영 상태·자격증명 무접촉). 각 항목은 담당자가 실패 테스트를 먼저 작성해 실패를 확인한 뒤 수정했고, 별도 리뷰어가 승인했다.

| ID | 재현 관측(de111b7) | PR / 머지 SHA | 먼저 실패한 테스트 | 수정 후 | 미해결·한계 |
|---|---|---|---|---|---|
| F1 사이징 분모≠실제 SL | SEPA ATR1% → 분모 4% → 175주, 실제 등록 SL 5% → 위험 0.875%(예산 +25%); ATR 없음 → 2.8 폴백 → 0.90% | #34 `15c2683` | `test_risk_sizing.py::test_f1_*`, `test_stop_policy.py` | `stop_policy.resolve_effective_stop` + `ExitManager.resolve_stop` 단일 창구, 엔진은 `_resolve_entry_stop` 콜백으로 실제 고정 SL(sepa 5/gap 3.5/vcp 4) 분모. 매수수수료 포함 계획 위험 ≤0.7% 최종 상한(139주, 140주는 9.85원 초과). 급락 cap 은 분모 미적용(보수적, 계획서 "동일 해석" 행의 의도적 편차) | 상한은 주문 시점 보장. 레짐 전환 시 `apply_regime_params` 가 SL 을 덮어쓰는 기존 청산 정책·갭·슬리피지는 원장 델타·canary 로 분류 |
| F2 진입 ATR 미래 봉 | 미래 고저만 바꾸면 손절 4.4→6.0%, risk 159→116주 | #35 `3defc82` | `test_backtest_point_in_time.py::test_f2_*` | `_entry_atr` 는 체결일 이전 확정 봉만(T+0 종가 체결만 당일 포함), 초기 손절·`initial_risk` 고정 | `atr_dynamic` 모드의 EOD 손절 판정은 기존대로 당일 ATR(연구 축, live_policy 무관) |
| F3 빈 응답 방어 우회 | 전량 매도 pending + 빈 응답 → 재조회 없이 pending 31분 종목 강제 삭제, sync 정상 보고 | #33 `4addac9` | `test_sync_portfolio_characterization.py::test_empty_reply_with_stale_sell_pending_is_retried` 외 | `empty_inconsistent`/`partial_missing` 분리, 재조회에도 빈 응답이면 `set_sync_status(False)` 로 포지션·익절 단계·현금·pending 보존 | — |
| F4 risk 태그 미전파 | 태그가 원본 Signal.metadata 에만, event/캐시/signal_events/원장에 없음 | #37 `6513149` + #40 `8f5580a` | `test_entry_risk_lifecycle.py`, `test_entry_risk_wiring.py` | `entry_risk` 스냅샷을 event.metadata·signal.metadata 양쪽 복사본·`_log_sig`·체결 원장 `market_context` 에 연결, `set_initial_risk`(최초 1회)·stage 영속, exporter·canary 왕복 | 완결 판정이 브로커 인메모리 `_pending_orders` 의존(재시작 직후 체결분은 canary 로 사후 확인), 취소 직전 ≤2초 증분 체결 누락 가능, config_hash 는 RiskConfig 만 |
| F5 하트비트 실패=성공 | DART 40분 5회 전부 실패해도 정체 아님, REST·진화도 동일 | #36 `28b7b72` | `test_loop_heartbeat_integration.py::test_dart_scheduler_all_failures_over_40min_*` 외 | `record_attempt/success/failure/idle/set_enabled/annotate`, last_success 는 성공만, 일일 잡 예정시각+60분 grace, `/api/health loop_status` | evolve() 이전 단계 예외 삼킴은 note 미표기; 예정시각 이후 재시작+지속 실패는 consecutive_failures 로만 노출; harvest/vol/DART 는 `_supervised` 없이 create_task(기존) |
| F6 재시도 등록 `_sync` 폴백 소실 | 재시도 kwargs 전부 None → config SL 로 재등록 | #33 `4addac9` | `test_sync_registration_failure_is_retried_by_fill_check_with_sync_params` | `_resolve_registration_params(strategy → _sync → {})` 단일 조회점, BUY 등록 예외도 대기열, 포지션 부재 3주기 연속에서만 정리 | BUY 최초 등록은 `.get(strategy, {})` 유지(엔진 해석기와 동일 규칙, 운영 전략은 전부 테이블에 있어 실효 차이 없음) |
| F7 게이트 기준군≠운영 | baseline 이 nominal 기본값이라 risk 0.7→0.8 무효, 미지원이 skipped 통과, WF 미평가 승인 | #38 `441bfd3` | `test_backtest_gate_config.py`(수정 전 12건 실패) | 유효 설정 builder·`unsupported`/`errored` 분리(evolver 는 unsupported 를 억제 대상으로)·WF 불가 보류·`budget_cap`·`slot_policy=live_weighted`·`risk_quantity_cap` parity | gap/VCP 백테스터 미구현 → SEPA 단독 부분 검증; parity xfail 2건 |
| F8 시가 주문의 종가 자산 평가 | 미래 종가만 바꾸면 신규 250→275주(nominal 기준군 포함) | #35 `3defc82` | `test_f8_future_close_of_held_symbol_does_not_change_open_fill_quantity` | `_calc_equity(phase="open")` — 시가 체결은 시가(봉 없는 보유 종목은 마지막 확정 종가) | — |

추가로 D 재현·리뷰 과정에서 확인해 수정한 것: 재시작 등록의 `price_history` 가 `List[Price]` 라 ExitManager dict API 와 불일치해 동적 손절이 어디서도 생기지 않음(기존 동작 — 고정 SL 이 실제 정책임을 확정), 캘린더 부스트가 위험 예산을 초과(→ 최종 상한), 강제 유령 정리 후 sync 정상 보고(→ F3), 백테스터 `check_exit` 의 매일 ATR 재계산(→ F2), 설정값 하나가 무효하면 청산 판정 전체가 스킵되던 fail-open(T2 통합 시 `update_price` 폴백), 급락 cap 이 사이징을 키우는 역효과(T2 통합 시 분모 미적용).

## 2. 전체 검증 결과와 검사 SHA

- **main `8f5580a`**: `QWQ_VERIFY_PYTHON=/home/ubuntu/projects/qwq-ai-trader/venv/bin/python bash scripts/dev/verify.sh` → 문법 검사 통과, **334 passed / 2 xfailed**(parity strict xfail: 손절 발동 기준 net-pnl vs 가격, 익절 일봉 고가 접촉), 비밀정보 검사 통과.
- PR 헤드마다 GitHub `verify` 체크 성공 후 머지(strict 보호). PR 업데이트 전의 성공 체크를 재사용하지 않았다(각 재푸시 SHA 의 check-run 을 폴링).
- 독립 리뷰: 모든 구현 브랜치에 구현자와 다른 리뷰어(xhigh). 1차 리뷰 verdict — T1 approve, T2 approve, T5 approve, T4 changes_requested(2), T3 changes_requested(2), T6 changes_requested(4), A-wiring changes_requested(1). blocking 은 전부 반영 후 재리뷰 approve(T4 는 CHANGELOG 만 잔여 → 통합 시 반영). `scripts/dev/codex_review.sh` 는 매번 `bwrap: loopback: Failed RTM_NEWADDR` 로 실패해 교차 리뷰 결과 없음 — 성공으로 기록하지 않았고 Claude 독립 리뷰가 대체 범위.
- 테스트 증가: 145(de111b7) → 334. 파일: sync 특성화 21, ExitManager 특성화 35, stop_policy·risk_sizing 76, point-in-time 16, backtest_gate_config 28, live_backtest_parity 15(+xfail 2), loop_heartbeat 24, entry_risk_lifecycle 24, entry_risk_wiring 15, risk_canary_report 18.

## 3. 유효 설정 snapshot·baseline/candidate 차이

- 유효 설정 hash(`load_effective_config`, default.yml + evolved_overrides.yml 병합, 비밀 제외): **`91a816502f3f`**.
- 이번 작업은 **운영 설정값을 바꾸지 않았다**. `risk.sizing_mode: risk`, `risk_per_trade_pct 0.7`, `risk_max_position_pct 18.0`, 전략 배분(sepa 40 / gap 15 / vcp 10 / core 0), ExitConfig(SL 5.0, min 4.0, max 8.0, TP1 10%/10%) 모두 de111b7 과 동일. `config/default.yml` 은 주석만 갱신.
- 게이트 baseline(유효 설정) vs candidate 는 요청 필드만 다르며 `GateResult.diff`/`config_hash` 로 원장에 남는다(F7 수정).
- 백테스트 A/B 입력: `results/ab_exit_policy_review_v2/manifest.json` — 통합 SHA `441bfd3`, 계산기 `2026-09-14.t7`, OHLCV 캐시 hash 62개, missing_tickers 0, 레짐 캐시 출처 `kospi_fdr`(1회 온라인 생성, 개별 종목 다운로드 0).

## 4. 저장 원자료에서 재계산한 A/B 결과와 운영 게이트 판정

`docs/research/risk-sizing-revalidation-2026-09.md`(상세) · 독립 리뷰가 raw positions/fills/equity 만으로 6/6 셀 재계산 일치, 원본 `results/ab_exit_policy_2026-09.json` 무변경 확인.

| 셀 (SEPA 단독, live_policy, live_weighted, 예산 캡 40%) | 6m 총수익 / MDD / WF / PF | 12m 총수익 / MDD / WF / PF | 운영 게이트 |
|---|---|---|---|
| nominal (기준군) | -7.45% / -11.1% / 0/3 / 0.76 | +4.20% / -12.8% / 0/3 / 1.07 | 기준 |
| risk 0.7%·상한 18% | -1.38% / -8.0% / 2/3 / 0.95 | +11.62% / -9.8% / 2/3 / 1.23 | 4조건 충족 |
| nominal14 (사전 등록 대조군, 고정 14%) | -1.87% / -8.0% / 3/3 / 0.93 | +6.68% / -10.0% / 2/3 / 1.13 | 4조건 충족 |

- **운영 게이트**(총수익 개선 >0pp·MDD 악화 ≤1pp·WF ≥2/3·거래 ≥10, `backtest_gate` 임계 그대로): risk 양 윈도우 충족. **그러나 대조군도 동일 통과** → 검증된 것은 노출 축소 효과(평균 노출 26~30→21~26%, 연 회전 39~41→33~34배, 비용 -12~15%, MDD 3pp)이며 위험 사이징 공식 고유의 기여는 분리되지 않는다. live_policy 에서 SEPA 진입 SL 이 5% 단일값이라 risk = equity×0.7%÷5% = **고정 14% 명목**(상한 18% 미발동, ATR 적응 효과 없음). risk 와 대조군의 차이는 매수수수료 포함 상한(1주 차이)·최소 주문금액 재검사(대조군은 미검사 — 백테스터 명목 경로 결함, parity 항목 등록)에서 시작된 경로 분기의 산물.
- **연구 판정**(기대값·PF 개선·WF 2/3·MDD ≤3pp): risk·대조군 양 윈도우 통과. 그러나 평균 R 6m +0.02 / 12m +0.12, 중앙 R 전 셀 -0.84(중앙값 거래는 손절), 상위 3건 제외 순손익 **전 셀 음수**, 전 셀 KOSPI 지수 대비 -26~-96pp(KODEX200 캐시 없음 → 초과수익 null) → **엣지 입증 아님**.
- **결정: 승격 보류.** 기술 필수 조건 중 parity xfail 2건(손절 기준·익절 접촉)이 남아 해당 범위 승격 불가이고, 대조군과 구분되지 않는다. 결과를 본 뒤 기준·셀·윈도우를 바꾸지 않았다(윈도우 시작 1~2 거래일 편차는 러너 산식 탓, 고지됨). 자동 nominal 복귀도 하지 않는다(계획서 §2.2).

## 5. 결론의 한계

- SEPA 단독 부분 검증 — 유효 배분 65% 중 40% 만 모사(gap/VCP 백테스터 미구현). KR 전체 정책 승인 근거가 될 수 없다.
- 6m·12m 구간이 겹친다 — 독립 검증 2회가 아니다.
- 일봉 체결 근사(같은 봉 손절 우선·갭 관통 시가 체결·종가 판정), 슬리피지·시장충격 미반영, 하드코딩 60종목 생존편향, 예산 캡 보유 노출은 진입원가×잔여비율 근사.
- parity 미해소 2건: 손절 발동 기준(백테스터 가격 하락률 vs 실엔진 수수료 포함 순손익률, ≈0.22%p 선발동), 익절 일봉 고가 접촉 vs 현재가.
- 이 구간에서 risk 의 분모가 상수(5%)라 "적응" 축 자체를 검증하지 못했다 — 전략별 SL 이 다른 gap/VCP 포함 그리드 또는 `entry_stop_mode=atr_dynamic` 연구 축이 별도 필요.
- 표본: 6m 포지션 56~68, 12m 113~146 — 기존 최소 거래 10건은 기술 하한이지 통계 신뢰성 기준이 아니다.

## 6. 운영 변경

**없음 — 배포 전.** 운영 서버(`/home/ubuntu/projects/qwq-ai-trader`, systemd `qwq-ai-trader`)는 09-13 배포본 **de111b7** 을 실행 중이다. 즉 F1(분모 불일치)·F3(빈 응답 방어 우회)·F5(하트비트) 등의 수정은 **운영에 반영되지 않았다**. 현금 0.4% 라 신규 매수 발동 가능성은 낮지만 계획서대로 이를 안전장치로 보지 않는다.

배포 실행 범위가 명시되면: 기존 절차(`/deploy-local`, 장외 시간, pending 가드·verify·헬스체크·자동 롤백)로 main `8f5580a` 이상을 반영하고, 계획서 §2.2 권고(검증 전 risk 경로 신규 진입 보류 — 매수 전용 킬스위치 `KILL_SWITCH_KR` 검토, 청산 유지)의 채택 여부를 배포안에 명시한다. 배포 후 기동 확인(KIS 연결·엔진 시작·`loop_status`)과 다음 거래일 09:00~09:30 장중 관측(KIS 코드별 오류·동기화 보류·pending age·하트비트 실패)을 별도 상태로 보고한다.

## 7. canary 상태

**미시작.** 도구는 준비됐다: `scripts/export_risk_ledger.py --source db` → `scripts/review_risk_canary.py --cohort risk-sepa_trend-v1`(runbook 절차). 원장의 `entry_risk` 는 배포 + 매수 재개 이후 체결부터 채워지며 그 전 거래는 legacy-unmeasured. 첫 5건은 technical_status, 10건은 재계산 불일치 0건, 30건은 status(`insufficient_sample`/`hold_expansion`/`further_review`) 로 보고하고, 어떤 결과에서도 nominal 자동 복귀를 하지 않는다.

## 8. 잔여 advisory (머지 차단 아님, 후속 과제)

- T1: 재시도 등록 영구 실패 시 알림 없음(warning 반복만); BUY 최초 등록 폴백 정책 명시.
- T2: `make_entry_stop_resolver` 가 dict 참조를 캡처(핫리로드 시 옛 dict) — 주석 계약; config_hash 에 ExitConfig·전략 SL 미포함.
- T3/A: 완결 판정의 인메모리 의존·취소 직전 증분 체결·같은 배치 취소+2차 매수 순서 경합(도달성 극히 낮음); canary 1원 허용치는 3 leg 이상 분할 매도에서 경계값.
- T4: evolve() 이전 단계 예외 note 미표기, `VOL_TARGETING` env 중복 판정, harvest/vol/DART 무감독 create_task.
- T6/T7: `_resolve_fields` 중복 필드·유효 설정 반복 로드, gate_replay 의 "재생 불가" 사유 단일, `--start-date`/`--regime-online-once` 옵션 부재, 평균노출 정의 summary.json 미포함, KODEX200 벤치마크 캐시 확보, T7-D 16셀 교호작용 미실행.
- 운영 서버 검증 환경: `codex_review.sh` 는 bwrap 샌드박스 오류로 사용 불가.

## 9. 다음 단계 제안 (사용자 결정 필요)

1. 배포 실행 범위 지시 여부(main `8f5580a`, 장외 시간) 및 검증 전 risk 신규 진입 보류 여부.
2. 위험 사이징의 고유 효과를 분리할 연구 축(gap/VCP 백테스터 구현 또는 `atr_dynamic` 연구 축)과 KODEX200 벤치마크 캐시 확보를 별도 연구 계획으로 등록할지.
3. 펩트론 087010(자산 99.6%)·현금 결정 — 매수 재개 없이는 canary 표본이 생기지 않는다.

## 10. T9 추가분 (2026-09-14 저녁, 모닝브리프↔실제 장 괴리 분석 반영)

계기: 09-14 07:01 모닝브리프 "반도체 중심 상승 갭 출발 가능성 높음" vs 실제 KOSPI -3.14% 출발·-3.26% 마감; 12:00 장중 레짐이 08:20 데이터를 재사용해 `trending_bull` 0.85; LLM 입력 필드명 불일치(S&P500·SOX·VIX 가 0 으로); 결측이 중립·0·높은 확신으로 포장. 급락 방어(09:05 crash, 09:30 SEPA 차단, 10:20 severe)는 작동.

| ID | 재현(D, main 5634fb8) | PR / 머지 SHA | 수정 |
|---|---|---|---|
| F9 12:00 재분류가 아침 캐시 재사용, 급락 상태 미전달 | get_kospi_change() 만 호출, `_intraday_state` 미참조 → LLM mock 이 캐시 수치로 bull 0.85 저장 | #45 `a5de546` | 분류 전 당일 지수 재조회·5/20일 재계산(당일 봉 이중 계상 가드), 급락 감지 상태(당일 갱신 시각 게이트) 프롬프트 포함, crash/severe 면 bull 미적용(`cap_regime_by_intraday_risk` 단일 출처), 입력마다 as_of·source, 결측은 '결측'·`missing_fields`, `regime_capped`/`confidence_raw` |
| F10 지수 키 불일치·VIX 미수집 | sp500=0(실제 -1.0), sox=0(실제 -3.2), vix=0 항상 | #43 `?`·#45 | 공급자 `US_INDEX_KEYS`·`^VIX` 수집·`indices_normalized`(결측 None+missing, 마감/조회 시각 분리), 소비자 별칭 조회 `_index_field`(없으면 None) |
| F11 브리프가 미국 자료로 한국장 갭 단정, 전문가 충돌 미검토 | 프롬프트 입력 5개 전부 US 파생, 출력 형식이 KOSPI 갭·대응 전략 요구 | #44 `fd58684` | `build_morning_brief` scope(us_close_only 면 "미국시장 마감 요약"·개장 단정 금지 + 응답 후 `sanitize_brief_claims`), 국내 자료(as_of)가 있을 때만 관찰 포인트+반대 근거, `build_expert_conflict_note` 상충 표시(07:30 결합부 배선 #45), 캐시 JSON 고정 스키마(kr_date/scope/inputs/claims) |
| F12 결측·오래된 자료가 중립/높은 확신 | 수급·공매도 빈 dict → score 0·conf 0.4; 수동 오버라이드 만료 검사 없음; 야간선물 as_of 없음 | #43 | `DataPoint`/`is_fresh`, ExpertOpinion `data_status`(insufficient ≤0.2·partial ≤0.7 — bear_consensus 임계와 동일)·`missing_inputs`, 집계에서 insufficient 제외 + MIN_VALID_EXPERTS=4 커버리지 게이트("자료 부족 N명" 07:30 표시 #45), 수동 오버라이드 `valid_until`(구 항목 mtime+14일), 야간선물 `fetched_at`/`value_changed_at`/`value_unchanged_minutes` |

- 요청 2(판단 시간 범위 분리): `RegimeHorizons`(open_expectation 09:30 만료·intraday_risk as_of 당일 게이트) + `effective_regime` — 장중 위험이 crash/severe 이면 bull 을 sideways 로만 강등(새 차단 아님), mid_trend 는 실시간 `update_regime` 단일 출처(리뷰 blocking: 오버라이드 세터가 실시간 판단을 영구히 가리던 것 제거). **운영에서 유효 레짐 캡이 처음 활성화되는 실경로**(5분 급락 루프 → `set_intraday_risk`).
- 요청 5(사후 평가): `morning_brief_eval.evaluate`(개장·종가·전문가 방향·업종 상대성과, 결측 hit=None, 브리프 kr_date≠평가일이면 미채점)·JSONL 원장·`summarize`("하루 결과로 규칙 변경 금지")·20:30 훅(120초 상한). 09-14 사례는 원장에 open=miss·close=miss 로 기록될 형태(배포 후부터 누적).
- 검증: main `a5de546` verify **456 passed / 2 xfailed**. 각 브랜치 독립 리뷰 → blocking 반영 → 재리뷰 approve(T9 A 는 CHANGELOG 만 잔여 → 통합 시 반영). 통합 시 재리뷰 advisory 반영: 어댑터 `effective_regime` 당일 게이트(시계 주입), KIS 폴백 이중 계상 가드, KOFR 루프 당일 스냅샷, 상충 문구 중복 방지, aggregate_bias 동표 NEUTRAL, '상승 갭 출발' 패턴.
- 실경로 의미 변경(문서화): apply_expert_adjustment score 경로가 유효 시장체제 전문가 <4명이면 무보정(bear_consensus 만 동작), partial 확신 상한 0.7(양방향), 유효 레짐 급락 캡 활성화. 주문·청산·급락 임계값·게이트 코드 무변경.
- 잔여: `kr_inputs` 미전달(야간선물 시장 시각 없음 → 운영 scope 는 us_close_only 유지), macro/us_market/kr_economy/global_micro/weekend 전문가 data_status 미설정, brief_tone/claims 휴리스틱(claims=None 비율로 확장 판단), `update_regime` 결측 0 채움(기존), v8 spark 시장 시각 미매핑, 팀 컨텍스트 자료부족 표시. **운영 미배포(de111b7)** — 배포 시 첫 장중 검증 포인트는 monitoring-checkpoints "장중 레짐 입력 신선도·급락 캡" 절.

## 11. T10 — T9 후속 교차 리뷰 수정 (2026-09-15, 연결 경로 일관성)

**목표:** 자료 수집 → 결측·신선도 검증 → 레짐 판단 → 실제 소비자 적용 → 장전 발송 → 장후 평가 의 연결 경로 일관성. 기준 main `dcec010`(리뷰 시점 = 최신 main, 후속 변경 없음) → 통합 브랜치 `feature/t10-crossreview`. 명세는 계획서 T10(F13~F22). "T10 코드 수정 완료" 이며 **운영 배포·예측 품질 개선 입증과는 별개**다.

### 11.1 안전 경계 실행 결과
- 운영 체크아웃(`/home/ubuntu/projects/qwq-ai-trader`, de111b7)·운영 캐시·`.env`·주문·킬스위치·systemd: 개발·검증 단계에서 무접촉. 배포는 §11.7 의 별도 실행 범위.
- 테스트 격리: `tests/conftest.py` 신설 — import 시점부터 루프백 외 socket **+ curl_cffi(yfinance 백엔드, C 레벨 curl 이라 socket 패치 우회 — D 재현 중 실측 발견)** 차단, `~/.cache/ai_trader(_us)`·운영 `.env`/logs/results 접근 차단(PermissionError, 테스트 ID 와 함께 세션 요약 출력), HOME 미변조. 기준 스위트에서 누출 4파일(체결 경로 레짐 캐시 stat 14건, ExitManager/DailyReportGenerator 생성자 mkdir 3건, 날짜 고정 테스트 1건) 수정 후 기준선 456 passed / 위반 0.

### 11.2 항목별 결과 (독립 재현 / 수정 / 인수 테스트 / 독립 리뷰 / 잔여)
| ID | D 독립 재현(기준 a59e29f) | 수정 | 인수 테스트 | 독립 리뷰 | 잔여 제한 |
|---|---|---|---|---|---|
| F13 정오 당일 봉 교체 | 재현 (c5 +4.0 유지) | 교체/추가/보류 3분기(`_today_bar_action`, `_prev_trading_day`), 현재 지수 as_of 와 봉 기준 as_of 분리, 로컬 계산으로 멱등 | `test_t10_regime_path.py` 7건 + 기존 2건 정정 + D 1건 + E2E | R-A 1회 승인, INT-1 승인 | 공휴일은 `is_kr_market_holiday` 기준(폴백 캘린더) |
| F14 최신 실측 급락 캡 | 재현 (감지기 normal/None 2변형) | `classify_intraday_level`/`max_intraday_level` 단일 규칙, 감지기·이번 조회·**어댑터 당일 관측** 3소스 보수 병합(분류기·30분 sync), 어댑터 역순/as_of 없는 값 거부, 파일 날짜·감지기 게이트 시계 `_now_kst` 통일 | 12건 + D 2건 + E2E 3건(전일 상태·역순·조회 실패) | R-A 승인 → **통합에서 D 재현이 어댑터 미병합 잔여 발견 → 수정** → INT-1 승인 | 완화 방향 덮어쓰기 없음. 12:00 이후 장중 회복 시 ExitManager 는 파일 실측(12:00) 기준 캡 유지·G2/사이징은 어댑터로 회복 — 보수적 비대칭(advisory) |
| F15 소비자 통일 | 재현 (stale 5→7 복귀, G2 복사본 bull) | `monitor_positions` 재적용 블록 제거(같은 `run_batch_scheduler` 루프의 30분 sync 로 일원화 — 시각 조건 없이 매 30분), G2 `_resolve_market_regime` 어댑터 우선 | 3건 + D 2건(실제 ExitManager·on_signal 인자 캡처) + E2E | R-A·INT-1 승인 | 레짐 파라미터 적용 주기 10분(원본)→30분(캡 반영); 급락 SL/TS 조임은 5분 감지기·재적용 블록으로 유지. `engine._market_regime` 복사본을 읽는 설명용 지점 2곳 잔존 |
| F16 무자료 전문가 | 재현 (valid_n 4·보정 +12) | 6명 data_status 판정(점수 규칙 입력 기준), `from_dict` unknown, 집계 allowlist(ok/partial) | 7건 + D 1건 | R-B 3라운드 승인(macro_context 만으로 partial 승격 blocking 등 4건 반영), INT-2 승인 | unknown 은 브리핑 문구에 합산; `us_market_expert` sox 당일값은 판정 대상 밖(advisory) |
| F17 신선도 연결 | 재현 (+12 가산·ok) | 세션 기반 as_of(`kr_night_futures_as_of`)·TTL·미래 거부, 전문가 게이트 | 11건 + D 2건 | R-B 승인, INT-2 승인 | **정책값**: KRX 야간 세션 18:00~05:00, 종료 후 as_of=05:00, 유효기간 다음 개장 전, 주말 확장, **공휴일 미반영**; yfinance 프록시 폴백(NKD/KS200=F)은 게이트 미적용 |
| F18 부분 응답 0 변환 | 재현 (price 0·missing False) | v7/v8 결측 None, price/change_pct 결측 심볼 quotes 제외+`_seen_missing` 사유, `missing_fields` | 7건 + D 3건 + E2E | R-B 승인(소비자 None 파손 blocking 반영), INT-2: **지수 4종 전부 결측 시 avg_pct=0 → "+0.00% 보합권 마감" blocking → 통합에서 수정** | v7 응답 성공·전 심볼 결측이면 v8 폴백 미시도 |
| F19 발송 판단 연결 | 재현 (전문가 축 미기록) | `record_morning_brief_dispatch`(C) + 07:30 **morning 슬롯만** 배선(A, `record_dispatch`) + 발송 스냅샷 우선 평가 | 4건(C)+5건(A)+D 1건+E2E | R-C 2라운드 승인 → **통합에서 아카이브 경로 규칙 불일치 발견 → 단일화** → INT-1/2 승인 | evaluated=False 레코드는 영구 고정(재평가 불가, advisory); 발송 valid_n 은 커버리지 기준으로 정정 |
| F20 테마 식별자 | 재현 (전부 미수집) | `BRIEF_THEME_EVAL_TARGETS`(AI/반도체→전기전자, 바이오→의약품), claims.sectors dict(theme/eval_targets/agg/supported/reason), `summarize` 제외 사유 집계 | 1건 + D 1건 + E2E | R-C·INT-2 승인 | 매핑 2건 외 unsupported(확장은 승인 필요), 업종명 완전일치 |
| F21 발송 범위 | 재현 (갭업 문구) | `brief_scope` 단일 판정, 고정 문구 시장·시간범위 분리, 지수 전부 결측이면 "지수 시세 미수집 — 판단 불가" | 2건 + 통합 1건 + D 1건 + E2E | R-C·INT-1 승인, INT-2 blocking 반영 | — |
| F22 원문 보존 | 재현 (원문 유실·참조 없음) | 날짜별 아카이브(generated/dispatch, raw_text·body_full·제거 문장·입력·모델), brief_ref, 과거일 실측 미수집, 원자적 쓰기·손상본 보존 | 3건 + D 1건 + E2E | R-C 승인(raw 절단 blocking 반영), INT-2 승인 | 아카이브 보존 정책 없음(하루 1파일 누적) |

### 11.3 통합 단계에서 D 재현·최종 리뷰가 잡은 것
- D 재현 6건이 통합 트리에서 실패 → 분류: **실제 잔여 결함 2건**(F14 어댑터 미병합, F19 아카이브 경로 이원화) 수정; **내부 표현 단정 4건**(F17 fetch 반환 형태, F18 quotes 포함 여부·주입 층, F20 sectors 문자열) 은 관찰 결과 기준으로 테스트 조정(근거를 테스트 주석에 기록).
- 최종 리뷰 INT-1(돈 경로, opus xhigh): 승인, blocking 0, advisory 9. INT-2(자료→평가, opus xhigh): blocking 1(지수 전부 결측 0 포장) → 수정·회귀 테스트 2건 추가. 반영한 advisory: 5분 루프 NaN 시 어댑터 재각인 방지, 발송 기록 valid_n 커버리지 기준, 아카이브 상수 이중 출처 제거. 미반영(잔여 기록): sox 당일값 판정, 12:00 이후 회복 비대칭, `guard_enabled=false` 시 캡 동반 비활성(기존 구조), 07:30 공휴일 가드, `_cache_ts` 갱신, 아카이브 보존 정책.
- D 통합 E2E(`tests/test_t10_e2e_flow.py` 8건): 07:00→07:30→12:00→20:30 고정 시계·메모리 공급자 — F13/F14/F15/F19/F20/F21 한 파이프라인 통과, 재시작·재실행 원장 1건, 전일 감지기·역순 도착·조회 실패·발송 실패·Yahoo 부분 응답·curl_cffi 차단 각 1건.

### 11.4 검증 (실제 실행)
- `venv/bin/python -m pytest tests -q -p no:cacheprovider` (통합 최종): **552 passed / 2 xfailed(parity strict, 기존) / 격리 위반 0건**. 구성: 기준 456 + A 27 + B 19 + C 10 + D 재현 14 + E2E 8 + 통합 회귀 등.
- D 재현 3파일 14건: 기준 a59e29f 에서 14 실패(기대) → 통합에서 14 통과.
- `scripts/dev/verify.sh`(문법·전체 테스트·비밀정보 검사): §11.7 커밋 후 실행 결과를 PR 본문에 기재.
- **Codex 교차 리뷰 미실행**: `scripts/dev/codex_review.sh --branch`(기준 origin/main) 가 `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted` 로 샌드박스 초기화에 실패(exit 0 이나 리뷰 없음). Claude opus/xhigh 독립 리뷰(브랜치별 R-A/R-B/R-C + 통합 2렌즈)로 대체 — Codex 성공을 주장하지 않는다.

### 11.5 축별 평가 가능 범위 (E2E 실측)
| 축 | 유효 표본 | 상태 |
|---|---|---|
| 개장 방향 | 0 | **정상 기권** — 운영 경로는 `kr_inputs` 미전달 → scope=us_close_only → 개장 주장 없음 |
| 종가 방향 | 0 | 정상 기권(동일 사유). 실측 종가는 수집됨 |
| 전문가 종합 방향 | 1 | 07:30 발송 스냅샷(+2 → flat) vs -3.26%(down) → hit False (결측 아님) |
| 언급 업종 상대성과 | 1 | AI/반도체→전기전자 평가, 2차전지는 "평가 대상 미합의" 로 제외 사유 기록 |
- 평가 레코드 수 ≠ 유효 표본 수: `summarize` 가 축별 n·제외 사유별 건수를 낸다.

### 11.6 임계값 변경 여부와 실제 동작 의미 변경
- 임계값·게이트 기준 변경: **없음**(두 최종 리뷰어 diff 확인, threshold_changes_found=[]).
- 의미 변경(문서화, CHANGELOG 동일): 급락 캡이 감지기 상태 외에 이번 조회 실측·어댑터 당일 관측에도 걸림 / 분류기가 어댑터에 관측을 밀어 넣어 G2·사이징이 분류 시점에 강등될 수 있음 / `monitor_positions` 재적용 제거(30분 sync 일원화) / G2 어댑터 유효 레짐 우선 / `update_intraday_state` 결측 시 상태 미갱신 / 6명 전문가 data_status 판정으로 partial 확신 상한 0.7 발동 범위 확대·`from_dict` unknown 집계 제외 / 야간선물은 세션 as_of 확인 시만 집계(낮 시간·yfinance 프록시 경로 차이) / Yahoo 결측 None 보존·결측 심볼 quotes 제외 / 07:00 문구 미국 세션 사실로 한정(지수 결측이면 미수집 표기) / claims.sectors 스키마 변경 / 모닝브리프 날짜별 아카이브 신설·최신 캐시에서 raw 제외.

### 11.7 PR 준비 상태와 승인 필요 사항
- PR: `feature/t10-crossreview` → main, 제목 "fix: T9 후속 교차 리뷰 수정 — 연결 경로 일관성 (계획서 T10, F13~F22)". 머지 전 필요: `verify` CI 통과.
- 사용자 승인이 필요한 정책값(코드 주석에 '정책 승인 필요' 로 표시): ① KRX 야간선물 세션 규칙(18:00~05:00, 공휴일 미반영) ② `ExpertOpinion.from_dict` 기본 unknown(구 저장 레코드 집계 제외) ③ 테마-업종 매핑 2건만 지원.
- 운영 배포·재기동: 사용자의 최종 지시("완료되면 문서업데이트 커밋 푸쉬 그리고 재기동까지 검증")를 실행 범위로 보고 §11.8 에 결과를 기록한다.

### 11.8 운영 변경 기록
- (커밋·PR·배포 후 갱신)
