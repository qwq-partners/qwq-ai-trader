# 실거래 KODEX200 초과수익 원장 설계 (설계 A)

> **상태 (2026-09-28):** 서면 설계만 있다. 구현·배포·재시작·실주문·설정 변경은 없다. 승인 범위는
> 작성자가 아닌 리뷰어의 **설계 승인**까지다. 구현은 별도 계획·PR·사용자 지시로 진행한다.
> 기준: main `e31c632`. 작성: Claude coordinator(세션 모델 Fable 5.1 — 라우팅 정책상 미자격이라 작성자 기록으로만 남긴다).

## 1. 목적과 판정 질문

엔진의 최종 목적은 "현행 위험 한도를 유지하며 **비용 차감 후 KODEX200 대비 초과수익**을 검증할 수 있는 엔진"이다.
`docs/reviews/strategy-architecture-review-2026-09.md` §0·§5(권고 5)는 04-23·08-20 결정이 절대수익 판정 때문에
뒤집혔다고 기록한다. 게이트 성적표(`gate_performance`)와 CF 추적은 이미 KODEX200 초과·손절 클립 기준으로 바뀌었다.
그러나 **실제로 체결된 왕복 포지션**에는 같은 기준으로 쌓이는 원장이 없다. 이 설계가 만드는 원장은 다음 질문에 답한다.

> 봇이 연 각 포지션은, 같은 돈을 같은 기간 KODEX200 에 두었을 때보다 비용을 다 낸 뒤 얼마나 더(덜) 벌었나?
> 손절 구조를 감안해 손실을 클립하면 그 값은 어떻게 달라지나?

원장은 **측정만** 한다. 자동 판정·승격·설정 변경·사이징 연결은 하지 않는다.

## 2. 이미 있는 자산과 재사용 결정

| 자산 | 지금 하는 일 | 이 설계에서의 쓰임 |
| --- | --- | --- |
| DB `trades` + `trade_events`(BUY/SELL 행) | 실거래 정본(리뷰 §1: KR 종결 266건, 03-09~) | **포지션 원천.** 리뷰가 "DB가 정본, 저널은 이벤트 단위 상향 편향"으로 확정했다 |
| `scripts/export_risk_ledger.py` `build_ledger` | DB → 포지션 원장. fills/exits leg, `net_pnl`=누적 `trade.pnl`, `lots_ambiguous`, `exits_aggregated`, `actual_stop_pct` | **그대로 재사용.** DB 조회 부분만 함수로 분리한다(§8) |
| `scripts/review_risk_canary.py` `position_benchmark` | 청산 수량 가중 동일기간 벤치마크 Σwᵢ(B[tᵢ]/B[t₀]−1), 없으면 null | **같은 식을 공용 함수로 옮긴다.** canary 와 이 원장이 구현 하나를 같이 쓴다 |
| `review_risk_canary.py` 판정 규칙 | 표본 <30 이면 `insufficient_sample`, 0 으로 채우지 않음 | 같은 최소 표본 30·null 규칙 |
| `src/analytics/gate_performance.py` `STOP_CLIP_PCT=-5.0`, `BENCH_SYMBOL="069500"` | 차단 후보의 클립·초과 판정 | **클립 수준(5%)만 재사용한다.** `forward_clipped` 는 신호일 종가~20영업일·비용 전·절대수익이라 이 설계의 x꜀ 와 직접 비교하지 않는다 |
| `src/analytics/counterfactual_tracker.py` | 20:30 블록에서 `broker.get_daily_prices("069500", days=45)` | 같은 브로커 함수로 벤치마크 캐시를 갱신한다(원천 통일) |
| `src/dashboard/data_collector.py:626-632` `is_sync` 규칙 | `SYNC_`/`KIS_SYNC_` 거래 id, `entry_reason=="sync_detected"`, 동기화 `exit_type` 4종 | 같은 규칙에 `sync_detected`(`trade_journal.py:512`)를 더해 진입·청산 양쪽 가격 품질 판정에 쓴다. 구현 때 공용 함수로 옮기고 data_collector 도 그 함수를 쓰게 한다 |
| `src/analytics/position_ledger.py` (`position_ledger.jsonl`) | 08-08~ 엔진 체결 경로의 포지션 원장 | **쓰지 않는다.** 청산 사유를 기록하지 않고(`engine.py:657` `on_sell` 에 reason 미전달), 동기화의 유령 삭제(`kr_scheduler.py:1387`)는 open 레코드를 확정하지 않는다. DB 와 원천을 이원화하지 않는다 |
| `src/utils/kospi_benchmark.py` | KOSPI 지수(^KS11) 로더 | 쓰지 않는다. 대상은 지수가 아니라 KODEX200 이고, FDR `069500` 에서 +24.2%/일 오염이 실측됐다(`volatility_targeting.py:47`) |

새로 만드는 것은 포지션 → 초과수익 행 계산, 벤치마크 캐시, 20:30 누적 단계, 요약 네 가지다.

## 3. 정의

포지션 p 는 exporter 가 만든 원소 하나다. 매수 leg 합계 원가 C, 매도 leg i 의 수량 qᵢ·체결일 dᵢ, 진입일 d₀.

| 기호 | 정의 | 근거·메모 |
| --- | --- | --- |
| `entry_cost` C | Σ(매수가×수량), 수수료 제외 | canary `entry_cost` 와 같다 |
| `net_pnl` N | DB 누적 `trade.pnl`. 매수 수수료·매도 수수료·거래세 포함 | `trade_journal.py:521-523` `calculate_net_pnl` 누적, exporter `_net_pnl` 정본 규칙 |
| `net_return` r | N / C | 비용 차감 수익률 |
| `bench_return` b | Σᵢ (qᵢ/Σq)·(B[dᵢ]/B[d₀] − 1) | B = KODEX200(069500) 일봉 종가. 분할 청산은 청산 수량 가중(canary 식) |
| `excess_return` x | r − b | **주 지표** |
| `excess_krw` X | N − C·b | 돈 가중. 같은 돈을 같은 방식으로 KODEX200 에 뒀을 때와의 원화 차이 |
| `clip_pct` s | `entry_risk.actual_stop_pct` → `entry_risk.stop_pct` → 공통 5.0(= −`STOP_CLIP_PCT`, 양수) | 앞의 둘은 09-13 이후 위험 사이징 스냅샷에 있다. 현재 설정으로 과거 손절폭을 추정하지 않는다(exporter 규칙과 같음). 상수는 음수(-5.0)이므로 부호를 뒤집어 쓴다 |
| `clip_basis` | `entry_stop` 또는 `common_5` | 두 기준은 해석이 다르다(아래) |
| `clipped_return` r꜀ | max(r, −s/100) | |
| `clipped_excess_return` x꜀ | r꜀ − b | 손절 구조를 감안한 초과수익. 클립 수준만 `gate_performance` 와 같다 |
| `stop_overshoot` o | r꜀ − r (≥0), 원화로는 o·C | **`entry_stop` 에서만 보고한다.** 계획 손절보다 더 잃은 폭(갭 하락·지연 손절·동기화 청산) |

- **벤치마크는 비용을 빼지 않은 가격 수익률이다.** KODEX200 매매 수수료(ETF 는 거래세 면제)를 빼지 않으므로
  전략에 불리한 쪽, 즉 보수적인 쪽이다. 분배금은 넣지 않는다(§10).
- **같은 기간**은 진입일 종가부터 각 청산일 종가까지다. 당일 왕복이면 b = 0 이다. 체결 시각의 장중 가격은 쓰지 않는다(§10).
- `common_5` 클립은 **비교용**이다. ATR 손절이 8% 였던 옛 포지션은 설계대로 청산돼도 r꜀ − r 이 3%p 로 나온다.
  그래서 이 기준의 `stop_overshoot` 는 집행 품질로 읽을 수 없고, 보고하지 않는다.
- 금액·비율은 `Decimal(str(x))` 로 계산해 문자열로 저장한다(프로젝트 규칙, canary 와 같음).

## 4. 흐름

```text
20:30 KR 진화 블록 끝(evolve 이후), 거래일만
 ├─ ① 벤치마크 캐시 전체 갱신: bot.broker.get_daily_prices("069500", days=N)   ← KIS 시세 TR 조회 1회/일
 │     N = 가장 오래된 포지션 진입일부터 오늘까지의 거래일 수(상한 500 = 브로커 5페이지 한도, 지금 약 140 → 2페이지)
 │     날짜 YYYYMMDD → YYYY-MM-DD 로 바꿔 ~/.cache/ai_trader/excess_return/kodex200_daily.csv
 │     (date,close,source,fetched_at) 를 **통째로 원자적 교체**한다 — 병합 규칙이 없으니 수정주가 소급 변경도 그대로 반영된다
 │     단, 받아 온 첫 날짜가 가장 오래된 진입일 이하일 때만 교체한다(N 에 +5 여유). 아니면 부분 응답(2페이지 실패 시
 │     1페이지만 반환, `kis_kr.py:1888-1891`)으로 보고 기존 캐시를 쓰고 로그를 남긴다
 ├─ ② 포지션 원장: exporter.fetch_trade_records(bot TradeStorage pool.fetch, days=730)
 │     → exporter.build_ledger(trades, exit_states={})   ← 종결 포지션만 쓰므로 ExitManager 상태가 필요 없다
 ├─ ③ 스냅샷: 모든 포지션을 position_row(pos, bench) 로 다시 계산 → positions.jsonl 을 **원자적 교체**
 └─ ④ 요약: 스냅샷 → summary.json(원자적 교체) + summary_history.jsonl 에 오늘 한 줄 append(fsync)
       drift = 직전 계산일(오늘과 날짜가 다른 마지막 스냅샷)과 비교해 net_pnl·bench_return 이 바뀐 포지션 수
토요일 후속복기 블록(게이트 성능 분석 다음): 요약 한 줄 텔레그램(요약 computed_at 날짜 포함)
```

- **누적의 의미:** 포지션 행은 매일 DB 에서 다시 계산한 스냅샷이다. 종결 포지션이 늘면 스냅샷에 자동으로 쌓이고,
  날짜별 흐름은 `summary_history.jsonl` 에 하루 한 줄씩 쌓인다. 한 번 쓴 행을 고정하지 않으므로, 분할 체결 진입이
  덜 팔린 상태에서 조기에 확정되는 일이 없고 DB 사후 수정(`kis_sync` 보정 등)도 다음 날 스냅샷에 그대로 반영된다.
- **봇 안에서 도는 이유:** 벤치마크 조회에는 봇의 브로커·리미터·토큰을 써야 한다. 별도 프로세스가 KIS 를 부르면
  초당 한도 합산이 끊기고 토큰 발급이 겹친다. DB 는 봇이 이미 가진 TradeStorage pool 을 쓰고, pool 이 없으면 그날은 건너뛴다.
  exporter 의 현재 `_load_from_db` 를 그대로 부르면 안 된다 — 그 함수가 `storage.connect()`/`disconnect()`(`export_risk_ledger.py:329·386`)로
  봇의 운영 pool 을 끊는다. 그래서 `fetch(pool.fetch)` 만 받는 함수로 분리한다(§8).
- **실행 시각:** 저녁 리포트(설정 `evening_report_time` 기본 17:00, `kr_scheduler.py:5151`)에서 `sync_from_kis`
  (`:5478-5479`)가 당일 동기화 청산을 DB 에 쓴 뒤다.
- **exporter 로드:** exporter 는 `scripts/` 에 있으므로 `backtest_gate.py:191`·`harvest_shadow.py:57` 처럼 파일 경로로 로드한다.
  checkout 만 앞당기고 재시작하지 않아 import 가 어긋나면 이 단계가 예외로 끝나고 로그만 남는다. 다음 재시작 후 복구된다.
  분석 단계라 매매에는 영향이 없다.
- **격리:** 단계 전체를 `asyncio.wait_for(…, 60)` 와 `try/except` 로 감싼다. 진화 뒤에 두므로 진화를 늦추지 않는다.

## 5. 행 스키마와 분류 규칙

`positions.jsonl` 한 줄이 포지션 하나다(schema 1). 매일 전체를 다시 쓴다. 아래 값은 합성 예시다.

```json
{
  "schema": 1, "position_id": "KR-…", "symbol": "005930", "strategy": "sepa_trend",
  "cohort_id": "risk-sepa_trend-v1", "applied_sha": "…",
  "entry_date": "2026-10-05", "last_exit_date": "2026-10-19", "holding_days": 14,
  "entry_cost": "1390000", "net_pnl": "41000", "fees_total_est": "3170",
  "net_return": "0.029496", "bench_return": "0.012000", "excess_return": "0.017496",
  "excess_krw": "24320", "clip_pct": "5.0", "clip_basis": "entry_stop",
  "clipped_return": "0.029496", "clipped_excess_return": "0.017496", "stop_overshoot": "0",
  "exit_types": ["take_profit", "trailing"], "entry_quality": "fill", "exit_quality": "fill",
  "exclusion": null, "bench_missing_reason": null,
  "bench_source": "KIS:FHKST03010100:069500:adj1", "computed_at": "2026-10-19T20:31:02+09:00", "code_sha": "…"
}
```

**종결 판정.** exporter 의 `closed`(`exit_quantity >= entry_quantity`, `export_risk_ledger.py:233`)만으로는 부족하다.
DB `trades.entry_quantity` 는 첫 체결 수량으로 고정되고(갱신하는 SQL 이 없다), 매수 leg 는 스냅샷의 누적 `filled_quantity`
를 쓰기 때문이다(`:141`). 그래서 행의 종결은 **`closed` 이고 Σ매도 leg 수량 ≥ Σ매수 leg 수량** 일 때로 정한다.
판정 순서: ① `closed` 인데 exits 가 없거나 `net_pnl` 이 없으면 `exits_missing` ② Σ매도 < Σ매수면 `awaiting_close`
③ Σ매도 > Σ매수면 `quantity_mismatch` ④ 나머지 제외 규칙. ①을 먼저 보지 않으면 exits 가 빈 포지션이 영원히 대기로 남는다.

| 구분 | 조건 | 처리 |
| --- | --- | --- |
| 미종결 | exporter `status=open` | 행을 쓰지 않는다 |
| 청산 진행 중 | `closed` 이지만 Σ매도 < Σ매수 | 행을 쓰지 않고 요약의 `awaiting_close` 로 센다 |
| `exits_missing` | 종결인데 exits 가 비었거나 `net_pnl` 이 없음 | 행은 쓰고 지표에서 뺀다(구조적 제외, 대기하지 않는다) |
| `quantity_mismatch` | Σ매도 > Σ매수 | 같음 |
| `lots_ambiguous` | 같은 종목의 보유 구간이 겹침(exporter) | 같음 |
| `exits_aggregated` | 분할 매도 leg 를 복원할 수 없음(exporter) | 같음. 청산일 가중을 할 수 없다 |
| `manual_entry` | `strategy == "manual"`(수동 풀매수, `kr_scheduler.py:7294`) | 같음. 봇이 판단한 진입이 아니다 |
| `sync_entry` | 거래 id 가 `KIS_SYNC_`/`SYNC_` 이거나 `entry_reason=="sync_detected"` | 같음. 동기화가 만든 진입은 전략이 기본값 `momentum_breakout` 으로 붙고(`trade_storage.py:995-1006`) 사용자 HTS 매수일 수도 있다 |
| `recovered_at_exit` | `entry_reason=="recovered_at_exit"` | 같음. 진입 시각이 청산 시각으로 기록돼(`trade_journal.py:474-484`) b=0 인 가짜 행이 된다 |
| `bench_out_of_range` | 진입일이 캐시 첫 날짜보다 이르다 | 같음 |
| 벤치마크 결측 | 진입일 또는 청산일 종가가 캐시에 없음(휴장일 오류 등) | 행은 쓰고 `bench_missing_reason`, x·X·x꜀ 는 null. 0 으로 채우지 않는다 |
| 동기화 청산 | 청산 leg 중 하나라도 동기화 `exit_type`(`kis_sync`·`sync_reconcile`·`sync_closed`·`sync_partial`·`sync_detected`) | **포함하고** `exit_quality="sync_estimated"`. 요약은 전체와 동기화 제외 두 값을 같이 낸다(리뷰 §1: 31건 가격 정확도 의문) |
| 사용자 청산 | `exit_type == "manual"` | 포함. `exit_types` 로 식별할 수 있다 |

제외 행도 쓰는 이유는, 무엇을 몇 건·얼마나 뺐는지를 원장만으로 재현하려는 것이다(§7 `excluded` 에 원화 합계를 같이 낸다).

**exporter 보정(구현 때):** `_fills_and_exits` 는 `exit_price <= 0` 이면 SELL leg 가 있어도 exits 를 비운다(`:162`).
DB 직접 부분매도 경로가 `exit_price`·`exit_time` 을 비워 두고 `exit_quantity` 만 누적하므로(`kr_scheduler.py:3050-3070`)
leg 가 있는 포지션이 `exits_missing` 으로 빠진다. 이 가드를 leg 가 없을 때의 폴백 경로에만 적용한다. canary 입력도 같이
좋아지는 수정이며 기존 canary 시험은 그대로 통과해야 한다.

## 6. 멱등·실패·호출 예산

- **멱등:** 스냅샷과 요약은 원자적 교체라 같은 날 두 번 돌아도 결과가 같다. `summary_history.jsonl` 은 같은 날짜 줄이
  이미 있으면 다시 쓰지 않는다.
- **drift:** 직전 계산일 스냅샷(같은 날 재실행이면 그날 첫 실행 전의 것)과 비교해 바뀐 포지션 수를 요약에 낸다. 그래서 같은 날 두 번 돌아도 요약과 이력 줄의 drift 가 같다. DB 사후 수정이나 수정주가 소급 변경이 여기서 드러난다.
- **실패:** 벤치마크 조회가 실패하면 기존 캐시로 진행한다(모자란 날짜는 `bench_missing_reason`). DB 가 없으면 그날은 건너뛰고
  스냅샷·요약을 그대로 둔다. 어떤 실패에서도 0 이나 빈 값으로 행을 쓰지 않는다. 로그 태그는 `[초과수익]`.
  토요일 한 줄에 요약의 `computed_at` 날짜를 넣어, 단계가 계속 실패해 옛 값이 나가는 것을 보이게 한다.
- **KIS 호출:** 시세 TR `FHKST03010100` 일봉 조회를 거래일마다 1회(지금 2페이지, 상한 5페이지) 부른다. `_api_get` 이
  `kis_rate_limit` 을 거친다. 원장 TR(`LEDGER_TR_IDS`)은 부르지 않으므로 EGW00215 와 무관하다(09-28 결정: EGW00215 는
  외부 조회와 겹친 것으로 수용, 조치 없음).
- 캐시 CSV 에는 `review_risk_canary.py --benchmark` 가 그대로 읽는 `date,close` 열(`YYYY-MM-DD`)이 들어 있다.

## 7. 요약과 보고

`summary.json` 은 창(window)과 묶음(group)마다 아래 지표를 낸다. 창은 전체와 최근 90일(마지막 청산일 기준)이다.
묶음은 전체 / 전략 / 월 / 청산 가격 품질(fill·sync_estimated) / cohort(legacy-unmeasured·risk-*) 다.

| 지표 | 정의 |
| --- | --- |
| `n`, `awaiting_close`, `drift` | 표본 회계 |
| `excluded{사유: {n, net_pnl_sum}}` | 제외 건수와 원화 합계. 손실이 제외군에 몰려 평균이 좋아 보이는 것을 드러낸다 |
| `mean_excess`, `median_excess` | x 의 평균·중앙값 |
| `excess_krw_sum`, `excess_krw_excl_top3` | X 합계, 상위 3건을 뺀 합계(꼬리 의존 확인, canary `net_pnl_excl_top3` 와 같은 뜻) |
| `t_excess` | mean/(sd/√n), n≥2. 참고값이다(§10: 동시 보유 상관으로 과대 추정). 리뷰 §6: 건당 σ≈5.85% 에서 +0.5% 를 검출하려면 약 530건 |
| `beat_rate` | x>0 비율 |
| `mean_net_return`, `mean_bench_return` | 베타 혼동을 드러내려고 둘 다 낸다 |
| `mean_clipped_excess` | x꜀ 평균(전체) |
| `overshoot_n`, `overshoot_krw_sum` | `entry_stop` 기준 o>0 건수와 원화 합 |
| `fees_total_est` | exporter 가 현재 수수료율로 다시 계산한 추정치(N 에 들어 있는 실제 비용과 다를 수 있다) |
| `status` | `insufficient_sample`(n<30) 또는 `measured`. **다른 값은 없다.** 통과·승격을 말하지 않는다 |

토요일 한 줄 예(합성 값): `📏 실거래 초과수익(KODEX200·비용 차감, 10-19 계산) n=41 평균 -0.42% 합계 -312,000원 t=-0.6 · 클립 -0.18% · 동기화 제외 n=35 -0.30% · 제외 6건 -85,000원 (표본 판정 보류)`.

## 8. 구현 때 바뀌는 파일 (다음 단계용)

| 파일 | 변경 |
| --- | --- |
| `src/analytics/excess_return.py` (신규) | `refresh_benchmark_cache(broker, path, since)`, `load_benchmark(path)`, `position_benchmark(bench, pos)`(canary 에서 이동), `is_sync_entry/is_sync_exit`(data_collector 규칙 + `sync_detected`), `position_row(pos, bench)`, `write_snapshot(path, rows)`, `summarize(rows, previous)`. import 는 표준 라이브러리와 loguru 만 쓰고, 브로커는 인자로 받는다 |
| `scripts/review_risk_canary.py` | `position_benchmark`·`load_benchmark` 를 위 모듈에서 import 한다(동작 동일). 기존 시험이 회귀 검사를 맡는다 |
| `scripts/export_risk_ledger.py` | `_load_from_db` 의 SELECT 두 개를 `fetch_trade_records(fetch, days)` 로 분리한다(`fetch` = `pool.fetch`, connect/disconnect 없음). `_fills_and_exits` 의 `exit_price` 가드를 leg 없는 폴백에만 적용한다(§5). CLI 동작은 가드 수정 외 동일 |
| `src/dashboard/data_collector.py` | `is_sync` 판정을 공용 함수로 바꾼다(`sync_detected` 추가로 대시보드 표시가 조금 넓어진다) |
| `src/schedulers/kr_scheduler.py` | 20:30 블록 끝에 ①~④ 한 호출(`wait_for` 60초, 예외 삼킴), 토요일 블록에 요약 한 줄 |
| 문서 | `docs/risk/risk-and-exit.md`(판정 기준 절), `docs/operations/monitoring-checkpoints.md`, `docs/operations/runbook.md`(파일 위치), CHANGELOG, CLAUDE.md 캐시 목록 |

주문·전략·위험 설정·`config/*.yml`·`.env`·킬스위치는 건드리지 않는다. 새 환경변수나 플래그도 만들지 않는다
(분석 단계이고 실패는 삼킨다).

## 9. 인수 시험

1. **손 계산 일치:** 단일 청산, 수량 가중 분할 청산(leg 2개), 당일 왕복(b=0), 벤치마크 날짜 누락(null과 사유, 0 아님),
   클립 `entry_stop`/`common_5`(상수 -5.0 → s=5.0, `max(r, -0.05)`), `common_5` 에서 overshoot 미보고, 동기화 진입·청산 표시.
2. **종결 판정:** 첫 체결 수량만큼만 팔린 다중 체결 진입은 `awaiting_close`(행 없음)이고, 전량 매도 뒤 다음 실행에서 정상 행이 된다.
   Σ매도 > Σ매수는 `quantity_mismatch`, `exit_price=0` 이지만 SELL leg 가 있는 부분매도 포지션은 leg 로 계산된다.
3. **분류:** 제외 사유 9종이 각각 한 번씩 나오고, 제외 원화 합계가 행 합계와 맞는다.
4. **공용 함수 이동의 무변경:** `tests/test_risk_canary_report.py`(특히 `test_benchmark_weighted_by_exit_quantity`),
   `tests/test_entry_risk_lifecycle.py`·`tests/test_entry_risk_wiring.py` 가 수정 없이 통과한다.
5. **회계 대조:** 합성 DB 행에서 Σ행 `net_pnl`(제외 포함, `awaiting_close` 제외) = Σ해당 `trades.pnl`(±1원).
6. **멱등·drift:** 두 번 실행해도 스냅샷·이력 줄 수가 같다. DB 행 하나를 바꾸면 다음 실행의 `drift` 가 1 이다.
7. **배선:** 스케줄러 단계가 진화 이후, `wait_for`·예외 삼킴 안에 있다. 가짜 브로커가 예외를 내도 블록이 끝까지 돈다.
   `fetch_trade_records` 가 pool 을 닫지 않는다.
8. **격리:** 시험은 `tmp_path` 캐시와 가짜 브로커만 쓴다(conftest 의 운영 캐시·네트워크 차단 준수).
9. 전체 회귀 UTC·KST 각 1회(조용한 시간대).

운영 첫 실행(배포는 별도 승인) 뒤 확인할 것: 스냅샷 행 수와 제외 건수·원화, 리뷰 §1 기간(03-09~07-02)의 원화 합계가
DB 합계와 맞는지, `bench_missing`·`awaiting_close` 가 설명 가능한 수준인지.

## 10. 알려진 한계·편향

- **일봉 근사:** 진입·청산을 그날 종가로 맞춘다. 보유 중앙값이 1.87일이라 개별 값의 잡음이 크다. 방향성 편향은 없다고
  가정하지만 확인하지 않았다(갭상승 전략은 장 초반에 산다).
- **분배금:** KODEX200 분배금(연 1~2% 수준)을 넣지 않는다. 분배락일을 걸친 포지션은 벤치마크가 낮게 잡혀 전략에 유리하다.
  KIS 수정주가(`fid_org_adj_prc=1`)가 ETF 분배를 반영하는지는 확인하지 않았다. `bench_source` 에 그대로 적는다.
- **종결 포지션만:** 손실을 오래 들고 있는 포지션은 빠지므로 결과가 좋은 쪽으로 기운다(처분효과).
- **상관:** 동시에 보유한 포지션은 같은 시장일을 공유하므로 `t_excess` 가 과대 추정된다.
- **포지션 단위:** 유휴 현금과 동시 보유 비중을 보지 않는다. 계좌 TWR 은 입출금이 기록되지 않아 계산할 수 없다(리뷰 §7).
- **동기화 가격:** `kis_sync` 류는 추정 가격이다. 그래서 전체와 동기화 제외 두 값을 같이 본다.
- **놓친 체결:** 17:00 `sync_from_kis` 는 당일 체결만 본다(`trade_storage.py:903-904`). 17시 이후 엔진이 놓친 체결(NXT 등)은
  다음 날에도 DB 에 복구되지 않는다. main 은 브로커 추적 목록 밖의 체결(재시작, 별도 프로세스 스크립트, 취소 직전 부분 체결)을
  동기화로만 반영한다 — 설계 B 가 다룬다.
- **수수료 추정:** `fees_total_est` 는 재계산 추정치다. 주 지표 N 은 DB 누적 손익이라 영향이 없다.
- **벤치마크 선택:** 대형주 지수라 중소형주 전략에 불리할 수 있다(리뷰 §7: 4·5월 -20~-30pp).
- **벤치마크 오류:** KIS 수정주가만 쓰고 이상치 검사를 두지 않는다(FDR 오염 대비 장치는 원천이 달라 뺐다). 교차 확인은 설계 B §9 T2(Toss, 관측 전용).
- **표본:** 현금 고갈(펩트론 비중) 동안에는 새 표본이 거의 없다. 당장의 가치는 03-09 이후 백필에 있다.

## 11. 하지 않는 것·설계 B 와의 경계

- 자동 판정·승격·사이징/게이트 연결, 설정·주문 변경, 새 원장 TR, Toss 사용, EGW00215 조치는 하지 않는다.
- `position_ledger.jsonl` 과의 이원 대조는 넣지 않는다. 필요해지면 drift 와 같은 방식으로 추가한다.
- 오프라인 CLI 는 만들지 않는다. 재현은 exporter JSON 과 공용 함수 시험으로 충분하다. 외부 검토자가 원하면 추가한다.
- 설계 B(단일 runtime owner)는 체결 → DB 기록을 writer 하나로 모은다. 그 전까지 이 원장은 현재 DB 를 정본으로 읽는다.
  B 이후에도 행 계약은 그대로 두고 원천만 owner 원장으로 바꾼다.
- 설계 B(브랜치 `feature/engine-b-minimal-kis-owner-design-20260928`)가 도입하는 거래일별 기록 완전성 표(`execution_day_status`)가
  생기면, `incomplete` 인 거래일을 건드린 포지션을 제외 사유 `record_incomplete` 로 뺀다. B 구현 전에는 해당 없음(현재 동작 변화 없음).

## 12. 리뷰 기록

| 회차 | 리뷰어(요청 모델/effort, 실제 모델) | 결과 | 처리 |
| --- | --- | --- | --- |
| 1 | 독립 설계 리뷰(요청 Claude Opus / high, 실제 모델·effort 미노출, 작성자 아님, 읽기 전용) — `edc1eb5` | APPROVE_WITH_CONDITIONS — P0 0, P1 1, P2 8 | P1-1(분할 체결 진입의 조기 확정·`exits_missing`) → 매일 전체 재계산 스냅샷으로 전환, 종결 = Σ매도 ≥ Σ매수, `awaiting_close`·`exits_missing` 추가, exporter `:162` 가드 수정. P2-1 → 진입·청산 양쪽 동기화 판정(`sync_detected` 포함), `sync_entry`·`recovered_at_exit` 제외. P2-2 → 제외 원화 합계. P2-3 → "클립 수준만 같다", s=−`STOP_CLIP_PCT`. P2-4 → 캐시 전체 교체·날짜 형식·`bench_out_of_range`. P2-5 → 토요일 줄에 계산일. P2-6 → 한계 3개 추가. P2-7 → `bench_suspect` 삭제. P2-8 → pool 을 닫지 않는 분리 명시. 인용 정정: 17:00 호출 `:5478-5479`, NXT 문장. `kr_scheduler.py:7294` 는 `grep` 으로 `strategy="manual"` 줄임을 재확인해 유지 |
| 2 | 같은 리뷰어 한정 재확인 — `b84b9a3` | **APPROVE** — 네 조건 충족, 새 P2 3건(권고) | P2-a `exits_missing` 판정 순서 명시, P2-b 부분 응답이면 캐시 교체 안 함(+5 여유), P2-c drift 비교 기준을 직전 계산일로 정의. `kr_scheduler.py:7294` 유지가 맞다고 리뷰어가 1차 지적을 철회 |
| 3 | (승인 후 메모, 리뷰 대상 아님) | — | §11 에 설계 B 의 `execution_day_status` → `record_incomplete` 경계 메모 한 줄 추가. 현재 계약·동작 변화 없음 |
